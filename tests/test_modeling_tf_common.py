# coding=utf-8
# Copyright 2019 HuggingFace Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.


import copy
import os
import random
import tempfile
import unittest

from transformers import is_tf_available, is_torch_available

from .utils import _tf_gpu_memory_limit, require_tf


if is_tf_available():
    import tensorflow as tf
    import numpy as np

    from transformers import tf_top_k_top_p_filtering

    if _tf_gpu_memory_limit is not None:
        gpus = tf.config.list_physical_devices("GPU")
        for gpu in gpus:
            # Restrict TensorFlow to only allocate x GB of memory on the GPUs
            try:
                tf.config.experimental.set_virtual_device_configuration(
                    gpu, [tf.config.experimental.VirtualDeviceConfiguration(memory_limit=_tf_gpu_memory_limit)]
                )
                logical_gpus = tf.config.experimental.list_logical_devices("GPU")
                print("Logical GPUs", logical_gpus)
            except RuntimeError as e:
                # Virtual devices must be set before GPUs have been initialized
                print(e)


def _config_zero_init(config):
    configs_no_init = copy.deepcopy(config)
    for key in configs_no_init.__dict__.keys():
        if "_range" in key or "_std" in key:
            setattr(configs_no_init, key, 0.0)
    return configs_no_init


@require_tf
class TFModelTesterMixin:

    model_tester = None
    all_model_classes = ()
    all_generative_model_classes = ()
    test_torchscript = True
    test_pruning = True
    test_resize_embeddings = True
    is_encoder_decoder = False

    def test_initialization(self):
        pass
        # config, inputs_dict = self.model_tester.prepare_config_and_inputs_for_common()

        # configs_no_init = _config_zero_init(config)
        # for model_class in self.all_model_classes:
        #     model = model_class(config=configs_no_init)
        #     for name, param in model.named_parameters():
        #         if param.requires_grad:
        #             self.assertIn(param.data.mean().item(), [0.0, 1.0],
        #             msg="Parameter {} of model {} seems not properly initialized".format(name, model_class))

    def test_save_load(self):
        config, inputs_dict = self.model_tester.prepare_config_and_inputs_for_common()

        for model_class in self.all_model_classes:
            model = model_class(config)
            outputs = model(inputs_dict)

            with tempfile.TemporaryDirectory() as tmpdirname:
                model.save_pretrained(tmpdirname)
                model = model_class.from_pretrained(tmpdirname)
                after_outputs = model(inputs_dict)

                # Make sure we don't have nans
                out_1 = after_outputs[0].numpy()
                out_2 = outputs[0].numpy()
                out_1 = out_1[~np.isnan(out_1)]
                out_2 = out_2[~np.isnan(out_2)]
                max_diff = np.amax(np.abs(out_1 - out_2))
                self.assertLessEqual(max_diff, 1e-5)

    def test_pt_tf_model_equivalence(self):
        if not is_torch_available():
            return

        import torch
        import transformers

        config, inputs_dict = self.model_tester.prepare_config_and_inputs_for_common()

        for model_class in self.all_model_classes:
            pt_model_class_name = model_class.__name__[2:]  # Skip the "TF" at the beggining
            pt_model_class = getattr(transformers, pt_model_class_name)

            config.output_hidden_states = True
            tf_model = model_class(config)
            pt_model = pt_model_class(config)

            # Check we can load pt model in tf and vice-versa with model => model functions
            tf_model = transformers.load_pytorch_model_in_tf2_model(tf_model, pt_model, tf_inputs=inputs_dict)
            pt_model = transformers.load_tf2_model_in_pytorch_model(pt_model, tf_model)

            # Check predictions on first output (logits/hidden-states) are close enought given low-level computational differences
            pt_model.eval()
            pt_inputs_dict = dict(
                (name, torch.from_numpy(key.numpy()).to(torch.long)) for name, key in inputs_dict.items()
            )
            with torch.no_grad():
                pto = pt_model(**pt_inputs_dict)
            tfo = tf_model(inputs_dict, training=False)
            tf_hidden_states = tfo[0].numpy()
            pt_hidden_states = pto[0].numpy()

            tf_nans = np.copy(np.isnan(tf_hidden_states))
            pt_nans = np.copy(np.isnan(pt_hidden_states))

            pt_hidden_states[tf_nans] = 0
            tf_hidden_states[tf_nans] = 0
            pt_hidden_states[pt_nans] = 0
            tf_hidden_states[pt_nans] = 0

            max_diff = np.amax(np.abs(tf_hidden_states - pt_hidden_states))
            # Debug info (remove when fixed)
            if max_diff >= 2e-2:
                print("===")
                print(model_class)
                print(config)
                print(inputs_dict)
                print(pt_inputs_dict)
            self.assertLessEqual(max_diff, 2e-2)

            # Check we can load pt model in tf and vice-versa with checkpoint => model functions
            with tempfile.TemporaryDirectory() as tmpdirname:
                pt_checkpoint_path = os.path.join(tmpdirname, "pt_model.bin")
                torch.save(pt_model.state_dict(), pt_checkpoint_path)
                tf_model = transformers.load_pytorch_checkpoint_in_tf2_model(tf_model, pt_checkpoint_path)

                tf_checkpoint_path = os.path.join(tmpdirname, "tf_model.h5")
                tf_model.save_weights(tf_checkpoint_path)
                pt_model = transformers.load_tf2_checkpoint_in_pytorch_model(pt_model, tf_checkpoint_path)

            # Check predictions on first output (logits/hidden-states) are close enought given low-level computational differences
            pt_model.eval()
            pt_inputs_dict = dict(
                (name, torch.from_numpy(key.numpy()).to(torch.long)) for name, key in inputs_dict.items()
            )
            with torch.no_grad():
                pto = pt_model(**pt_inputs_dict)
            tfo = tf_model(inputs_dict)
            tfo = tfo[0].numpy()
            pto = pto[0].numpy()
            tf_nans = np.copy(np.isnan(tfo))
            pt_nans = np.copy(np.isnan(pto))

            pto[tf_nans] = 0
            tfo[tf_nans] = 0
            pto[pt_nans] = 0
            tfo[pt_nans] = 0

            max_diff = np.amax(np.abs(tfo - pto))
            self.assertLessEqual(max_diff, 2e-2)

    def test_compile_tf_model(self):
        config, inputs_dict = self.model_tester.prepare_config_and_inputs_for_common()

        if self.is_encoder_decoder:
            input_ids = {
                "decoder_input_ids": tf.keras.Input(batch_shape=(2, 2000), name="decoder_input_ids", dtype="int32"),
                "encoder_input_ids": tf.keras.Input(batch_shape=(2, 2000), name="encoder_input_ids", dtype="int32"),
            }
        else:
            input_ids = tf.keras.Input(batch_shape=(2, 2000), name="input_ids", dtype="int32")
        optimizer = tf.keras.optimizers.Adam(learning_rate=3e-5, epsilon=1e-08, clipnorm=1.0)
        loss = tf.keras.losses.SparseCategoricalCrossentropy(from_logits=True)
        metric = tf.keras.metrics.SparseCategoricalAccuracy("accuracy")

        for model_class in self.all_model_classes:
            # Prepare our model
            model = model_class(config)

            # Let's load it from the disk to be sure we can use pretrained weights
            with tempfile.TemporaryDirectory() as tmpdirname:
                outputs = model(inputs_dict)  # build the model
                model.save_pretrained(tmpdirname)
                model = model_class.from_pretrained(tmpdirname)

            outputs_dict = model(input_ids)
            hidden_states = outputs_dict[0]

            # Add a dense layer on top to test intetgration with other keras modules
            outputs = tf.keras.layers.Dense(2, activation="softmax", name="outputs")(hidden_states)

            # Compile extended model
            extended_model = tf.keras.Model(inputs=[input_ids], outputs=[outputs])
            extended_model.compile(optimizer=optimizer, loss=loss, metrics=[metric])

    def test_keyword_and_dict_args(self):
        config, inputs_dict = self.model_tester.prepare_config_and_inputs_for_common()

        for model_class in self.all_model_classes:
            model = model_class(config)
            outputs_dict = model(inputs_dict)

            inputs_keywords = copy.deepcopy(inputs_dict)
            input_ids = inputs_keywords.pop("input_ids" if not self.is_encoder_decoder else "decoder_input_ids", None,)
            outputs_keywords = model(input_ids, **inputs_keywords)

            output_dict = outputs_dict[0].numpy()
            output_keywords = outputs_keywords[0].numpy()

            self.assertLess(np.sum(np.abs(output_dict - output_keywords)), 1e-6)

    def test_attention_outputs(self):
        config, inputs_dict = self.model_tester.prepare_config_and_inputs_for_common()

        decoder_seq_length = (
            self.model_tester.decoder_seq_length
            if hasattr(self.model_tester, "decoder_seq_length")
            else self.model_tester.seq_length
        )
        encoder_seq_length = (
            self.model_tester.encoder_seq_length
            if hasattr(self.model_tester, "encoder_seq_length")
            else self.model_tester.seq_length
        )
        decoder_key_length = (
            self.model_tester.key_length if hasattr(self.model_tester, "key_length") else decoder_seq_length
        )
        encoder_key_length = (
            self.model_tester.key_length if hasattr(self.model_tester, "key_length") else encoder_seq_length
        )

        for model_class in self.all_model_classes:
            config.output_attentions = True
            config.output_hidden_states = False
            model = model_class(config)
            outputs = model(inputs_dict)
            attentions = [t.numpy() for t in outputs[-1]]
            self.assertEqual(model.config.output_attentions, True)
            self.assertEqual(model.config.output_hidden_states, False)
            self.assertEqual(len(attentions), self.model_tester.num_hidden_layers)
            self.assertListEqual(
                list(attentions[0].shape[-3:]),
                [self.model_tester.num_attention_heads, encoder_seq_length, encoder_key_length],
            )
            out_len = len(outputs)

            if self.is_encoder_decoder:
                self.assertEqual(out_len % 2, 0)
                decoder_attentions = outputs[(out_len // 2) - 1]
                self.assertEqual(model.config.output_attentions, True)
                self.assertEqual(model.config.output_hidden_states, False)
                self.assertEqual(len(decoder_attentions), self.model_tester.num_hidden_layers)
                self.assertListEqual(
                    list(decoder_attentions[0].shape[-3:]),
                    [self.model_tester.num_attention_heads, decoder_seq_length, decoder_key_length],
                )

            # Check attention is always last and order is fine
            config.output_attentions = True
            config.output_hidden_states = True
            model = model_class(config)
            outputs = model(inputs_dict)
            self.assertEqual(out_len + (2 if self.is_encoder_decoder else 1), len(outputs))
            self.assertEqual(model.config.output_attentions, True)
            self.assertEqual(model.config.output_hidden_states, True)

            attentions = [t.numpy() for t in outputs[-1]]
            self.assertEqual(len(attentions), self.model_tester.num_hidden_layers)
            self.assertListEqual(
                list(attentions[0].shape[-3:]),
                [self.model_tester.num_attention_heads, encoder_seq_length, encoder_key_length],
            )

    def test_hidden_states_output(self):
        config, inputs_dict = self.model_tester.prepare_config_and_inputs_for_common()

        for model_class in self.all_model_classes:
            config.output_hidden_states = True
            config.output_attentions = False
            model = model_class(config)
            outputs = model(inputs_dict)
            hidden_states = [t.numpy() for t in outputs[-1]]
            self.assertEqual(model.config.output_attentions, False)
            self.assertEqual(model.config.output_hidden_states, True)
            self.assertEqual(len(hidden_states), self.model_tester.num_hidden_layers + 1)
            self.assertListEqual(
                list(hidden_states[0].shape[-2:]), [self.model_tester.seq_length, self.model_tester.hidden_size],
            )

    def test_model_common_attributes(self):
        config, inputs_dict = self.model_tester.prepare_config_and_inputs_for_common()

        for model_class in self.all_model_classes:
            model = model_class(config)
            assert isinstance(model.get_input_embeddings(), tf.keras.layers.Layer)
            x = model.get_output_embeddings()
            assert x is None or isinstance(x, tf.keras.layers.Layer)

    def test_determinism(self):
        config, inputs_dict = self.model_tester.prepare_config_and_inputs_for_common()

        for model_class in self.all_model_classes:
            model = model_class(config)
            first, second = (
                model(inputs_dict, training=False)[0],
                model(inputs_dict, training=False)[0],
            )
            out_1 = first.numpy()
            out_2 = second.numpy()
            out_1 = out_1[~np.isnan(out_1)]
            out_2 = out_2[~np.isnan(out_2)]
            max_diff = np.amax(np.abs(out_1 - out_2))
            self.assertLessEqual(max_diff, 1e-5)

    def _get_embeds(self, wte, input_ids):
        # ^^ In our TF models, the input_embeddings can take slightly different forms,
        # so we try a few of them.
        # We used to fall back to just synthetically creating a dummy tensor of ones:
        try:
            x = wte(input_ids, mode="embedding")
        except Exception:
            try:
                x = wte([input_ids], mode="embedding")
            except Exception:
                try:
                    x = wte([input_ids, None, None, None], mode="embedding")
                except Exception:
                    if hasattr(self.model_tester, "embedding_size"):
                        x = tf.ones(input_ids.shape + [self.model_tester.embedding_size], dtype=tf.dtypes.float32,)
                    else:
                        x = tf.ones(input_ids.shape + [self.model_tester.hidden_size], dtype=tf.dtypes.float32,)
        return x

    def test_inputs_embeds(self):
        config, inputs_dict = self.model_tester.prepare_config_and_inputs_for_common()
        if not self.is_encoder_decoder:
            input_ids = inputs_dict["input_ids"]
            del inputs_dict["input_ids"]
        else:
            encoder_input_ids = inputs_dict["encoder_input_ids"]
            decoder_input_ids = inputs_dict["decoder_input_ids"]
            del inputs_dict["encoder_input_ids"]
            del inputs_dict["decoder_input_ids"]

        for model_class in self.all_model_classes:
            model = model_class(config)

            wte = model.get_input_embeddings()
            if not self.is_encoder_decoder:
                inputs_dict["inputs_embeds"] = self._get_embeds(wte, input_ids)
            else:
                inputs_dict["encoder_inputs_embeds"] = self._get_embeds(wte, encoder_input_ids)
                inputs_dict["decoder_inputs_embeds"] = self._get_embeds(wte, decoder_input_ids)

            model(inputs_dict)

    def test_lm_head_model_random_generate(self):

        config, inputs_dict = self.model_tester.prepare_config_and_inputs_for_common()
        input_ids = inputs_dict.get(
            "input_ids", None
        )  # TODO (PVP): ugly workaround to make code work for t5 for the moment - has to changed when t5 is fixed.

        for model_class in self.all_generative_model_classes:
            # TODO (PVP): add beam search tests when beam search is implemented
            model = model_class(config)

            if config.bos_token_id is None:
                with self.assertRaises(AssertionError):
                    model.generate(max_length=5)
                # batch_size = 1
                self._check_generated_tokens(model.generate(input_ids))
            else:
                # batch_size = 1
                self._check_generated_tokens(model.generate(max_length=5))
                # batch_size = 1, num_beams > 1

            # batch_size > 1, sample
            self._check_generated_tokens(model.generate(input_ids, num_return_sequences=3))
            # batch_size > 1, greedy
            self._check_generated_tokens(model.generate(input_ids, do_sample=False, num_return_sequences=3))

    def _check_generated_tokens(self, output_ids):
        for token_id in output_ids[0].numpy().tolist():
            self.assertGreaterEqual(token_id, 0)
            self.assertLess(token_id, self.model_tester.vocab_size)


def ids_tensor(shape, vocab_size, rng=None, name=None, dtype=None):
    """Creates a random int32 tensor of the shape within the vocab size."""
    if rng is None:
        rng = random.Random()

    total_dims = 1
    for dim in shape:
        total_dims *= dim

    values = []
    for _ in range(total_dims):
        values.append(rng.randint(0, vocab_size - 1))

    output = tf.constant(values, shape=shape, dtype=dtype if dtype is not None else tf.int32)

    return output


@require_tf
class UtilsFunctionsTest(unittest.TestCase):

    # tests whether the top_k_top_p_filtering function behaves as expected
    def test_top_k_top_p_filtering(self):
        logits = tf.convert_to_tensor(
            [
                [
                    8.2220991,  # 3rd highest value; idx. 0
                    -0.5620044,
                    5.23229752,
                    4.0386393,
                    -6.8798378,
                    -0.54785802,
                    -3.2012153,
                    2.92777176,
                    1.88171953,
                    7.35341276,  # 5th highest value; idx. 9
                    8.43207833,  # 2nd highest value; idx. 10
                    -9.85711836,
                    -5.96209236,
                    -1.13039161,
                    -7.1115294,
                    -0.8369633,
                    -5.3186408,
                    7.06427407,
                    0.81369344,
                    -0.82023817,
                    -5.9179796,
                    0.58813443,
                    -6.99778438,
                    4.71551189,
                    -0.18771637,
                    7.44020759,  # 4th highest value; idx. 25
                    9.38450987,  # 1st highest value; idx. 26
                    2.12662941,
                    -9.32562038,
                    2.35652522,
                ],  # cummulative prob of 5 highest values <= 0.6
                [
                    0.58425518,
                    4.53139238,
                    -5.57510464,
                    -6.28030699,
                    -7.19529503,
                    -4.02122551,
                    1.39337037,
                    -6.06707057,
                    1.59480517,
                    -9.643119,
                    0.03907799,
                    0.67231762,
                    -8.88206726,
                    6.27115922,  # 4th highest value; idx. 13
                    2.28520723,
                    4.82767506,
                    4.30421368,
                    8.8275313,  # 2nd highest value; idx. 17
                    5.44029958,  # 5th highest value; idx. 18
                    -4.4735794,
                    7.38579536,  # 3rd highest value; idx. 20
                    -2.91051663,
                    2.61946077,
                    -2.5674762,
                    -9.48959302,
                    -4.02922645,
                    -1.35416918,
                    9.67702323,  # 1st highest value; idx. 27
                    -5.89478553,
                    1.85370467,
                ],  # cummulative prob of 5 highest values <= 0.6
            ],
            dtype=tf.float32,
        )

        non_inf_expected_idx = tf.convert_to_tensor(
            [[0, 0], [0, 9], [0, 10], [0, 25], [0, 26], [1, 13], [1, 17], [1, 18], [1, 20], [1, 27]], dtype=tf.int32,
        )  # expected non filtered idx as noted above

        non_inf_expected_output = tf.convert_to_tensor(
            [8.222099, 7.3534126, 8.432078, 7.4402075, 9.38451, 6.271159, 8.827531, 5.4402995, 7.3857956, 9.677023],
            dtype=tf.float32,
        )  # expected non filtered values as noted above

        output = tf_top_k_top_p_filtering(logits, top_k=10, top_p=0.6, min_tokens_to_keep=4)

        non_inf_output = output[output != -float("inf")]
        non_inf_idx = tf.cast(
            tf.where(tf.not_equal(output, tf.constant(-float("inf"), dtype=tf.float32))), dtype=tf.int32,
        )

        tf.debugging.assert_near(non_inf_output, non_inf_expected_output, rtol=1e-12)
        tf.debugging.assert_equal(non_inf_idx, non_inf_expected_idx)
