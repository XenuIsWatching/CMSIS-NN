#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright 2010-2023 Arm Limited and/or its affiliates <open-source-office@arm.com>
#
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the License); you may
# not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an AS IS BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
import os
import sys
import json
import math
import argparse
import subprocess
import numpy as np

from packaging import version
from abc import ABC, abstractmethod

try:
    import tensorflow as tf
except Exception as e:
    print(e)
    sys.exit(1)

REQUIRED_MINIMUM_TENSORFLOW_VERSION = version.parse("2.10")

CLANG_FORMAT = 'clang-format-12 -i'  # For formatting generated headers.

INT32_MAX = 2147483647
INT32_MIN = -2147483648
INT64_MAX = 9223372036854775807
INT64_MIN = -9223372036854775808
INT16_MAX = 32767
INT16_MIN = -32768
INT8_MAX = 127
INT8_MIN = -128


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate input and refererence output data for unittests."
                                     " It can regenerate all data, load all stored data or a combination of it.")
    parser.add_argument('--dataset', type=str, default=None, help="Name of generated test set.")
    parser.add_argument('--regenerate-weights', action='store_true', help="Regenerate and store new weights.")
    parser.add_argument('--regenerate-input', action='store_true', help="Regenerate and store new input.")
    parser.add_argument('--regenerate-biases', action='store_true', help="Regenerate and store new biases.")
    parser.add_argument('-a', '--regenerate-all', action='store_true', help="Regenerate and store all data.")
    parser.add_argument('-t',
                        '--testtype',
                        type=str,
                        default=None,
                        choices=[
                            'conv', 'depthwise_conv', 'avgpool', 'maxpool', 'fully_connected', 'softmax', 'svdf', 'add',
                            'mul', 'lstm'
                        ],
                        help='Type of test. There are the operators that have unit tests.')
    parser.add_argument('--run-all-testsets',
                        action='store_true',
                        help="Run the script for all existing test "
                        "sets. Regenerate all, partially all or no input data (output may still change, depending on"
                        " changes in script) depending on regenerate flags. If used together with the -t flag, only"
                        " tests of that type will be run.")
    parser.add_argument('--schema-file', type=str, help="Path to schema file. This may be needed for some tests.")

    args = parser.parse_args()
    return args


# Use interpreter from tensorflow or tflite_runtime.
# Set this to False to use the tflite_runtime instead.
# See README for more info.
default_interpreter = True

if default_interpreter:
    from tensorflow.lite.python.interpreter import Interpreter
    from tensorflow.lite.python.interpreter import OpResolverType
else:
    from tflite_runtime.interpreter import Interpreter
    from tflite_runtime.interpreter import OpResolverType
    import tflite_runtime as tfl_runtime


class TestSettings(ABC):

    # This is the generated test data used by the test cases.
    OUTDIR = 'TestCases/TestData/'

    # This is input to the data generation. If everything or something is regenerated then it is overwritten.
    # So it always has the same data as the OUTDIR.
    # The purpose of the pregen is primarily for debugging, as it is enabling to change a single parameter and see how
    # output changes (or not changes), without regenerating all input data.
    # It also convinient when testing changes in the script, to be able to run all test sets again.
    PREGEN = 'PregeneratedData/'

    def __init__(self,
                 dataset,
                 testtype,
                 regenerate_weights,
                 regenerate_input,
                 regenerate_biases,
                 schema_file,
                 in_ch,
                 out_ch,
                 x_in,
                 y_in,
                 w_x,
                 w_y,
                 stride_x=1,
                 stride_y=1,
                 pad=False,
                 randmin=INT8_MIN,
                 randmax=INT8_MAX,
                 batches=1,
                 generate_bias=True,
                 relu6=False,
                 out_activation_min=None,
                 out_activation_max=None,
                 int16xint8=False,
                 bias_min=INT32_MIN,
                 bias_max=INT32_MAX,
                 dilation_x=1,
                 dilation_y=1):

        self.tensor_flow_reference_version = (
            "// Generated by {} using tensorflow version {} (Keras version {}).\n".format(
                os.path.basename(__file__), tf.__version__, tf.keras.__version__))

        if 'tflite_runtime' in sys.modules:
            revision = tfl_runtime.__git_version__
            version = tfl_runtime.__version__
            interpreter = "tflite_runtime"
        else:
            revision = tf.__git_version__
            version = tf.__version__
            interpreter = "tensorflow"

        self.tensor_flow_reference_version += ("// Interpreter from {} version {} and revision {}.\n".format(
            interpreter, version, revision))

        # Randomization interval
        self.mins = randmin
        self.maxs = randmax

        self.bias_mins = bias_min
        self.bias_maxs = bias_max

        self.input_ch = in_ch
        self.output_ch = out_ch
        self.x_input = x_in
        self.y_input = y_in
        self.filter_x = w_x
        self.filter_y = w_y
        self.stride_x = stride_x
        self.stride_y = stride_y
        self.dilation_x = dilation_x
        self.dilation_y = dilation_y
        self.batches = batches
        self.test_type = testtype
        self.has_padding = pad

        self.is_int16xint8 = int16xint8

        if relu6:
            self.out_activation_max = 6
            self.out_activation_min = 0
        else:
            if out_activation_min is not None:
                self.out_activation_min = out_activation_min
            else:
                self.out_activation_min = INT16_MIN if self.is_int16xint8 else INT8_MIN
            if out_activation_max is not None:
                self.out_activation_max = out_activation_max
            else:
                self.out_activation_max = INT16_MAX if self.is_int16xint8 else INT8_MAX

        # Bias is optional.
        self.generate_bias = generate_bias

        self.generated_header_files = []
        self.pregenerated_data_dir = self.PREGEN

        self.config_data = "config_data.h"

        self.testdataset = dataset

        self.kernel_table_file = self.pregenerated_data_dir + self.testdataset + '/' + 'kernel.txt'
        self.inputs_table_file = self.pregenerated_data_dir + self.testdataset + '/' + 'input.txt'
        self.bias_table_file = self.pregenerated_data_dir + self.testdataset + '/' + 'bias.txt'

        if self.has_padding:
            self.padding = 'SAME'
        else:
            self.padding = 'VALID'

        self.regenerate_new_weights = regenerate_weights
        self.regenerate_new_input = regenerate_input
        self.regenerate_new_bias = regenerate_biases
        self.schema_file = schema_file

        self.headers_dir = self.OUTDIR + self.testdataset + '/'
        os.makedirs(self.headers_dir, exist_ok=True)

        self.model_path = "{}model_{}".format(self.headers_dir, self.testdataset)
        self.model_path_tflite = self.model_path + '.tflite'

        self.input_data_file_prefix = "input"
        self.weight_data_file_prefix = "weights"
        self.bias_data_file_prefix = "biases"
        self.output_data_file_prefix = "output_ref"

    def save_multiple_dim_array_in_txt(self, file, data):
        header = ','.join(map(str, data.shape))
        np.savetxt(file, data.reshape(-1, data.shape[-1]), header=header, delimiter=',')

    def load_multiple_dim_array_from_txt(self, file):
        with open(file) as f:
            shape = list(map(int, next(f)[1:].split(',')))
            data = np.genfromtxt(f, delimiter=',').reshape(shape)
        return data.astype(np.float32)

    def convert_tensor_np(self, tensor_in, converter, *qminmax):
        w = tensor_in.numpy()
        shape = w.shape
        w = w.ravel()
        if len(qminmax) == 2:
            fw = converter(w, qminmax[0], qminmax[1])
        else:
            fw = converter(w)
        fw.shape = shape
        return tf.convert_to_tensor(fw)

    def convert_tensor(self, tensor_in, converter, *qminmax):
        w = tensor_in.numpy()
        shape = w.shape
        w = w.ravel()
        normal = np.array(w)
        float_normal = []

        for i in normal:
            if len(qminmax) == 2:
                float_normal.append(converter(i, qminmax[0], qminmax[1]))
            else:
                float_normal.append(converter(i))

        np_float_array = np.asarray(float_normal)
        np_float_array.shape = shape

        return tf.convert_to_tensor(np_float_array)

    def get_randomized_data(self, dims, npfile, regenerate, decimals=0, minrange=None, maxrange=None):
        if not minrange:
            minrange = self.mins
        if not maxrange:
            maxrange = self.maxs
        if not os.path.exists(npfile) or regenerate:
            regendir = os.path.dirname(npfile)
            os.makedirs(regendir, exist_ok=True)
            if decimals == 0:
                data = tf.Variable(tf.random.uniform(dims, minval=minrange, maxval=maxrange, dtype=tf.dtypes.int64))
                data = tf.cast(data, dtype=tf.float32)
            else:
                data = tf.Variable(tf.random.uniform(dims, minval=minrange, maxval=maxrange, dtype=tf.dtypes.float32))
                data = np.around(data.numpy(), decimals)
                data = tf.convert_to_tensor(data)

            print("Saving data to {}".format(npfile))
            self.save_multiple_dim_array_in_txt(npfile, data.numpy())
        else:
            print("Loading data from {}".format(npfile))
            data = tf.convert_to_tensor(self.load_multiple_dim_array_from_txt(npfile))
        return data

    def get_randomized_input_data(self, input_data, input_shape=None):
        # Generate or load saved input data unless hardcoded data provided
        if input_shape is None:
            input_shape = [self.batches, self.y_input, self.x_input, self.input_ch]
        if input_data is not None:
            input_data = tf.reshape(input_data, input_shape)
        else:
            input_data = self.get_randomized_data(input_shape,
                                                  self.inputs_table_file,
                                                  regenerate=self.regenerate_new_input)
        return input_data

    def get_randomized_bias_data(self, biases):
        # Generate or load saved bias data unless hardcoded data provided
        if not self.generate_bias:
            biases = tf.reshape(np.full([self.output_ch], 0), [self.output_ch])
        elif biases is not None:
            biases = tf.reshape(biases, [self.output_ch])
        else:
            biases = self.get_randomized_data([self.output_ch],
                                              self.bias_table_file,
                                              regenerate=self.regenerate_new_bias,
                                              minrange=self.bias_mins,
                                              maxrange=self.bias_maxs)
        return biases

    def format_output_file(self, file):
        command_list = CLANG_FORMAT.split(' ')
        command_list.append(file)
        try:
            process = subprocess.run(command_list)
            if process.returncode != 0:
                print(f"ERROR: {command_list = }")
                sys.exit(1)
        except Exception as e:
            raise RuntimeError(f"{e} from: {command_list = }")

    def write_c_header_wrapper(self):
        filename = "test_data.h"
        filepath = self.headers_dir + filename

        print("Generating C header wrapper {}...".format(filepath))
        with open(filepath, 'w+') as f:
            f.write(self.tensor_flow_reference_version)
            while len(self.generated_header_files) > 0:
                f.write('#include "{}"\n'.format(self.generated_header_files.pop()))
        self.format_output_file(filepath)

    def write_common_config(self, f, prefix):
        """
        Shared by conv/depthwise_conv and pooling
        """
        f.write("#define {}_FILTER_X {}\n".format(prefix, self.filter_x))
        f.write("#define {}_FILTER_Y {}\n".format(prefix, self.filter_y))
        f.write("#define {}_STRIDE_X {}\n".format(prefix, self.stride_x))
        f.write("#define {}_STRIDE_Y {}\n".format(prefix, self.stride_y))
        f.write("#define {}_PAD_X {}\n".format(prefix, self.pad_x))
        f.write("#define {}_PAD_Y {}\n".format(prefix, self.pad_y))
        f.write("#define {}_OUTPUT_W {}\n".format(prefix, self.x_output))
        f.write("#define {}_OUTPUT_H {}\n".format(prefix, self.y_output))

    def write_c_common_header(self, f):
        f.write(self.tensor_flow_reference_version)
        f.write("#pragma once\n")

    def write_c_config_header(self, write_common_parameters=True) -> None:
        filename = self.config_data

        self.generated_header_files.append(filename)
        filepath = self.headers_dir + filename

        prefix = self.testdataset.upper()

        print("Writing C header with config data {}...".format(filepath))
        with open(filepath, "w+") as f:
            self.write_c_common_header(f)
            if (write_common_parameters):
                f.write("#define {}_OUT_CH {}\n".format(prefix, self.output_ch))
                f.write("#define {}_IN_CH {}\n".format(prefix, self.input_ch))
                f.write("#define {}_INPUT_W {}\n".format(prefix, self.x_input))
                f.write("#define {}_INPUT_H {}\n".format(prefix, self.y_input))
                f.write("#define {}_DST_SIZE {}\n".format(prefix, self.x_output * self.y_output * self.output_ch *
                                                          self.batches))
                f.write("#define {}_INPUT_SIZE {}\n".format(prefix, self.x_input * self.y_input * self.input_ch))
                f.write("#define {}_OUT_ACTIVATION_MIN {}\n".format(prefix, self.out_activation_min))
                f.write("#define {}_OUT_ACTIVATION_MAX {}\n".format(prefix, self.out_activation_max))
                f.write("#define {}_INPUT_BATCHES {}\n".format(prefix, self.batches))
        self.format_output_file(filepath)

    def get_data_file_name_info(self, name_prefix) -> (str, str):
        filename = name_prefix + "_data.h"
        filepath = self.headers_dir + filename
        return filename, filepath

    def generate_c_array(self, name, array, datatype="int8_t", const="const ") -> None:
        w = None

        if type(array) is list:
            w = array
            size = len(array)
        elif type(array) is np.ndarray:
            w = array
            w = w.ravel()
            size = w.size
        else:
            w = array.numpy()
            w = w.ravel()
            size = tf.size(array)

        filename, filepath = self.get_data_file_name_info(name)
        self.generated_header_files.append(filename)

        print("Generating C header {}...".format(filepath))
        with open(filepath, "w+") as f:
            self.write_c_common_header(f)
            f.write("#include <stdint.h>\n\n")
            if size > 0:
                f.write(const + datatype + " " + self.testdataset + '_' + name + "[%d] =\n{\n" % size)
                for i in range(size - 1):
                    f.write("  %d,\n" % w[i])
                f.write("  %d\n" % w[size - 1])
                f.write("};\n")
            else:
                f.write(const + datatype + " *" + self.testdataset + '_' + name + " = NULL;\n")
        self.format_output_file(filepath)

    def set_output_dims_and_padding(self, output_x, output_y):
        self.x_output = output_x
        self.y_output = output_y
        if self.has_padding:
            # Take dilation into account.
            filter_x = (self.filter_x - 1) * self.dilation_x + 1
            filter_y = (self.filter_y - 1) * self.dilation_y + 1

            pad_along_width = max((self.x_output - 1) * self.stride_x + filter_x - self.x_input, 0)
            pad_along_height = max((self.y_output - 1) * self.stride_y + filter_y - self.y_input, 0)
            pad_top = pad_along_height // 2
            pad_left = pad_along_width // 2
            self.pad_x = pad_left
            self.pad_y = pad_top
        else:
            self.pad_x = 0
            self.pad_y = 0

    @abstractmethod
    def generate_data(self, input_data=None, weights=None, biases=None) -> None:
        ''' Must be overriden '''

    def quantize_scale(self, scale):
        significand, shift = math.frexp(scale)
        significand_q31 = round(significand * (1 << 31))
        return significand_q31, shift

    def get_calib_data_func(self, n_inputs, shape):

        def representative_data_gen():
            representative_testsets = []
            if n_inputs > 0:
                for i in range(n_inputs):
                    representative_testsets.append(np.ones(shape, dtype=np.float32))
                yield representative_testsets
            else:
                raise RuntimeError("Invalid number of representative test sets: {}. Must be more than 0".format(
                    self.test_type))

        return representative_data_gen

    def convert_and_interpret(self, model, inttype, input_data=None, dataset_shape=None) -> Interpreter:
        """
        Compile and convert a model to Tflite format, run interpreter and allocate tensors.
        """
        model.compile(loss=tf.keras.losses.categorical_crossentropy,
                      optimizer=tf.keras.optimizers.Adam(),
                      metrics=['accuracy'])
        n_inputs = len(model.inputs)

        if dataset_shape:
            representative_dataset_shape = dataset_shape
        else:
            representative_dataset_shape = (self.batches, self.y_input, self.x_input, self.input_ch)

        converter = tf.lite.TFLiteConverter.from_keras_model(model)

        representative_dataset = self.get_calib_data_func(n_inputs, representative_dataset_shape)

        converter.optimizations = [tf.lite.Optimize.DEFAULT]
        converter.representative_dataset = representative_dataset
        if self.is_int16xint8:
            converter.target_spec.supported_ops = [
                tf.lite.OpsSet.EXPERIMENTAL_TFLITE_BUILTINS_ACTIVATIONS_INT16_WEIGHTS_INT8
            ]
        else:
            converter.target_spec.supported_ops = [tf.lite.OpsSet.TFLITE_BUILTINS_INT8]
        converter.inference_input_type = inttype
        converter.inference_output_type = inttype
        tflite_model = converter.convert()

        os.makedirs(os.path.dirname(self.model_path_tflite), exist_ok=True)
        with open(self.model_path_tflite, "wb") as model:
            model.write(tflite_model)

        interpreter = Interpreter(model_path=str(self.model_path_tflite),
                                  experimental_op_resolver_type=OpResolverType.BUILTIN_REF)
        interpreter.allocate_tensors()

        output_details = interpreter.get_output_details()
        (self.output_scale, self.output_zero_point) = output_details[0]['quantization']

        if input_data is not None:
            input_details = interpreter.get_input_details()
            (self.input_scale, self.input_zero_point) = input_details[0]['quantization']

            # Set input tensors
            interpreter.set_tensor(input_details[0]["index"], tf.cast(input_data, inttype))

        return interpreter

    def generate_json_from_template(self, weights_feature_data=None, weights_time_data=None, bias_data=None):
        """
        Takes a json template and parameters as input and creates a new json file.
        """
        generated_json_file = self.model_path + '.json'

        with open(self.json_template, 'r') as in_file, open(generated_json_file, 'w') as out_file:
            # Update shapes, scales and zero points
            data = in_file.read()
            for item, to_replace in self.json_replacements.items():
                data = data.replace(item, str(to_replace))

            data = json.loads(data)

            # Update weights and bias data
            if weights_feature_data is not None:
                w_1_buffer_index = 1
                data["buffers"][w_1_buffer_index]["data"] = self.to_bytes(weights_feature_data.numpy().ravel(), 1)
            if weights_time_data is not None:
                w_2_buffer_index = 2
                data["buffers"][w_2_buffer_index]["data"] = self.to_bytes(weights_time_data.numpy().ravel(), 2)
            if bias_data is not None:
                bias_buffer_index = 3
                data["buffers"][bias_buffer_index]["data"] = self.to_bytes(bias_data.numpy().ravel(), 4)

            json.dump(data, out_file, indent=2)

        return generated_json_file

    def flatc_generate_tflite(self, json_input, schema):
        flatc = 'flatc'
        if schema is None:
            raise RuntimeError("A schema file is required.")
        command = "{} -o {} -c -b {} {}".format(flatc, self.headers_dir, schema, json_input)
        command_list = command.split(' ')
        try:
            process = subprocess.run(command_list)
            if process.returncode != 0:
                print(f"ERROR: {command = }")
                sys.exit(1)
        except Exception as e:
            raise RuntimeError(f"{e} from: {command = }. Did you install flatc?")

    def to_bytes(self, tensor_data, type_size) -> bytes:
        result_bytes = []

        if type_size == 1:
            tensor_type = np.uint8
        elif type_size == 2:
            tensor_type = np.uint16
        elif type_size == 4:
            tensor_type = np.uint32
        else:
            raise RuntimeError("Size not supported: {}".format(type_size))

        for val in tensor_data:
            for byte in int(tensor_type(val)).to_bytes(type_size, 'little'):
                result_bytes.append(byte)

        return result_bytes


class ConvSettings(TestSettings):

    def __init__(self,
                 dataset,
                 testtype,
                 regenerate_weights,
                 regenerate_input,
                 regenerate_biases,
                 schema_file,
                 in_ch=1,
                 out_ch=1,
                 x_in=7,
                 y_in=7,
                 w_x=3,
                 w_y=3,
                 stride_x=2,
                 stride_y=2,
                 pad=True,
                 randmin=INT8_MIN,
                 randmax=INT8_MAX,
                 batches=1,
                 generate_bias=True,
                 relu6=False,
                 out_activation_min=None,
                 out_activation_max=None,
                 int16xint8=False,
                 bias_min=INT32_MIN,
                 bias_max=INT32_MAX,
                 dilation_x=1,
                 dilation_y=1):
        super().__init__(dataset,
                         testtype,
                         regenerate_weights,
                         regenerate_input,
                         regenerate_biases,
                         schema_file,
                         in_ch,
                         out_ch,
                         x_in,
                         y_in,
                         w_x,
                         w_y,
                         stride_x,
                         stride_y,
                         pad,
                         randmin,
                         randmax,
                         batches,
                         generate_bias=generate_bias,
                         relu6=relu6,
                         out_activation_min=out_activation_min,
                         out_activation_max=out_activation_max,
                         int16xint8=int16xint8,
                         bias_min=bias_min,
                         bias_max=bias_max,
                         dilation_x=dilation_x,
                         dilation_y=dilation_y)

        self.scaling_factors = []

        if self.test_type == 'depthwise_conv':
            self.channel_multiplier = self.output_ch // self.input_ch
            if self.output_ch % self.input_ch != 0:
                raise RuntimeError("out channel ({}) is not multiple of in channel ({})".format(out_ch, in_ch))

    def write_c_config_header(self) -> None:
        super().write_c_config_header()

        filename = self.config_data
        filepath = self.headers_dir + filename
        prefix = self.testdataset.upper()

        with open(filepath, "a") as f:
            self.write_common_config(f, prefix)
            if self.test_type == 'depthwise_conv':
                f.write("#define {}_CH_MULT {}\n".format(prefix, self.channel_multiplier))
            f.write("#define {}_INPUT_OFFSET {}\n".format(prefix, -self.input_zero_point))
            f.write("#define {}_OUTPUT_OFFSET {}\n".format(prefix, self.output_zero_point))
            f.write("#define {}_DILATION_X {}\n".format(prefix, self.dilation_x))
            f.write("#define {}_DILATION_Y {}\n".format(prefix, self.dilation_y))

    def generate_quantize_per_channel_multiplier(self):
        num_channels = self.output_ch
        per_channel_multiplier = []
        per_channel_shift = []

        if len(self.scaling_factors) != num_channels:
            raise RuntimeError("Missing scaling factors")

        for i in range(num_channels):
            effective_output_scale = self.input_scale * self.scaling_factors[i] / self.output_scale
            (quantized_multiplier, shift) = self.quantize_scale(effective_output_scale)

            per_channel_multiplier.append(quantized_multiplier)
            per_channel_shift.append(shift)

        return per_channel_multiplier, per_channel_shift

    def generate_data(self, input_data=None, weights=None, biases=None) -> None:
        if self.is_int16xint8:
            inttype = tf.int16
            datatype = "int16_t"
            bias_datatype = "int64_t"
        else:
            inttype = tf.int8
            datatype = "int8_t"
            bias_datatype = "int32_t"

        input_data = self.get_randomized_input_data(input_data)

        if self.test_type == 'conv':
            out_channel = self.output_ch
        elif self.test_type == 'depthwise_conv':
            out_channel = self.channel_multiplier

        if weights is not None:
            weights = tf.reshape(weights, [self.filter_y, self.filter_x, self.input_ch, out_channel])
        else:
            weights = self.get_randomized_data([self.filter_y, self.filter_x, self.input_ch, out_channel],
                                               self.kernel_table_file,
                                               minrange=INT32_MIN,
                                               maxrange=INT32_MAX,
                                               decimals=1,
                                               regenerate=self.regenerate_new_weights)

        biases = self.get_randomized_bias_data(biases)

        # Create a one layer Keras model.
        model = tf.keras.models.Sequential()
        input_shape = (self.batches, self.y_input, self.x_input, self.input_ch)
        model.add(tf.keras.layers.InputLayer(input_shape=input_shape[1:], batch_size=self.batches))
        if self.test_type == 'conv':
            conv_layer = tf.keras.layers.Conv2D(self.output_ch,
                                                kernel_size=(self.filter_y, self.filter_x),
                                                strides=(self.stride_y, self.stride_x),
                                                padding=self.padding,
                                                input_shape=input_shape[1:],
                                                dilation_rate=(self.dilation_y, self.dilation_x))
            model.add(conv_layer)
            conv_layer.set_weights([weights, biases])
        elif self.test_type == 'depthwise_conv':
            depthwise_layer = tf.keras.layers.DepthwiseConv2D(kernel_size=(self.filter_y, self.filter_x),
                                                              strides=(self.stride_y, self.stride_x),
                                                              padding=self.padding,
                                                              depth_multiplier=self.channel_multiplier,
                                                              input_shape=input_shape[1:],
                                                              dilation_rate=(self.dilation_y, self.dilation_x))
            model.add(depthwise_layer)
            depthwise_layer.set_weights([weights, biases])
        interpreter = self.convert_and_interpret(model, inttype, input_data)

        all_layers_details = interpreter.get_tensor_details()
        filter_layer = all_layers_details[2]
        bias_layer = all_layers_details[1]
        if weights.numpy().size != interpreter.get_tensor(filter_layer['index']).size or \
           (self.generate_bias and biases.numpy().size != interpreter.get_tensor(bias_layer['index']).size):
            raise RuntimeError(f"Dimension mismatch for {self.testdataset}")

        output_details = interpreter.get_output_details()
        self.set_output_dims_and_padding(output_details[0]['shape'][2], output_details[0]['shape'][1])

        self.generate_c_array(self.input_data_file_prefix, input_data, datatype=datatype)
        self.generate_c_array(self.weight_data_file_prefix, interpreter.get_tensor(filter_layer['index']))

        self.scaling_factors = filter_layer['quantization_parameters']['scales']
        per_channel_multiplier, per_channel_shift = self.generate_quantize_per_channel_multiplier()
        self.generate_c_array("output_mult", per_channel_multiplier, datatype='int32_t')
        self.generate_c_array("output_shift", per_channel_shift, datatype='int32_t')

        self.generate_c_array(self.bias_data_file_prefix, interpreter.get_tensor(bias_layer['index']), bias_datatype)

        # Generate reference
        interpreter.invoke()
        output_data = interpreter.get_tensor(output_details[0]["index"])
        self.generate_c_array(self.output_data_file_prefix,
                              np.clip(output_data, self.out_activation_min, self.out_activation_max),
                              datatype=datatype)

        self.write_c_config_header()
        self.write_c_header_wrapper()


class PoolingSettings(TestSettings):

    def __init__(self,
                 dataset,
                 testtype,
                 regenerate_weights,
                 regenerate_input,
                 regenerate_biases,
                 schema_file,
                 channels=8,
                 x_in=4,
                 y_in=4,
                 w_x=4,
                 w_y=4,
                 stride_x=1,
                 stride_y=1,
                 randmin=INT8_MIN,
                 randmax=INT8_MAX,
                 bias_min=INT32_MIN,
                 bias_max=INT32_MAX,
                 batches=1,
                 pad=False,
                 relu6=False,
                 out_activation_min=None,
                 out_activation_max=None,
                 int16xint8=False):
        super().__init__(dataset,
                         testtype,
                         regenerate_weights,
                         regenerate_input,
                         regenerate_biases,
                         schema_file,
                         channels,
                         channels,
                         x_in,
                         y_in,
                         w_x,
                         w_y,
                         stride_x,
                         stride_y,
                         pad,
                         randmin=randmin,
                         randmax=randmax,
                         relu6=relu6,
                         out_activation_min=out_activation_min,
                         out_activation_max=out_activation_max,
                         int16xint8=int16xint8)

    def generate_data(self, input_data=None) -> None:
        if self.is_int16xint8:
            datatype = "int16_t"
            inttype = tf.int16
        else:
            datatype = "int8_t"
            inttype = tf.int8

        input_data = self.get_randomized_input_data(input_data)
        self.generate_c_array(self.input_data_file_prefix, input_data, datatype=datatype)

        input_data = tf.cast(input_data, tf.float32)

        # Create a one-layer Keras model
        model = tf.keras.models.Sequential()
        input_shape = (self.batches, self.y_input, self.x_input, self.input_ch)
        model.add(tf.keras.layers.InputLayer(input_shape=input_shape[1:], batch_size=self.batches))
        if self.test_type == 'avgpool':
            model.add(
                tf.keras.layers.AveragePooling2D(pool_size=(self.filter_y, self.filter_x),
                                                 strides=(self.stride_y, self.stride_x),
                                                 padding=self.padding,
                                                 input_shape=input_shape[1:]))
        elif self.test_type == 'maxpool':
            model.add(
                tf.keras.layers.MaxPooling2D(pool_size=(self.filter_y, self.filter_x),
                                             strides=(self.stride_y, self.stride_x),
                                             padding=self.padding,
                                             input_shape=input_shape[1:]))
        else:
            raise RuntimeError("Wrong test type")

        interpreter = self.convert_and_interpret(model, inttype, input_data)

        output_details = interpreter.get_output_details()
        self.set_output_dims_and_padding(output_details[0]['shape'][2], output_details[0]['shape'][1])

        # Generate reference
        interpreter.invoke()
        output_data = interpreter.get_tensor(output_details[0]["index"])
        self.generate_c_array(self.output_data_file_prefix,
                              np.clip(output_data, self.out_activation_min, self.out_activation_max),
                              datatype=datatype)

        self.write_c_config_header()
        self.write_c_header_wrapper()

    def write_c_config_header(self) -> None:
        super().write_c_config_header()

        filename = self.config_data
        filepath = self.headers_dir + filename
        prefix = self.testdataset.upper()

        with open(filepath, "a") as f:
            self.write_common_config(f, prefix)


class FullyConnectedSettings(TestSettings):

    def __init__(self,
                 dataset,
                 testtype,
                 regenerate_weights,
                 regenerate_input,
                 regenerate_biases,
                 schema_file,
                 in_ch=1,
                 out_ch=1,
                 x_in=1,
                 y_in=1,
                 w_x=1,
                 w_y=1,
                 stride_x=1,
                 stride_y=1,
                 pad=False,
                 randmin=INT8_MIN,
                 randmax=INT8_MAX,
                 batches=1,
                 generate_bias=True,
                 out_activation_min=None,
                 out_activation_max=None,
                 int16xint8=False,
                 bias_min=INT32_MIN,
                 bias_max=INT32_MAX):
        super().__init__(dataset,
                         testtype,
                         regenerate_weights,
                         regenerate_input,
                         regenerate_biases,
                         schema_file,
                         in_ch,
                         out_ch,
                         x_in,
                         y_in,
                         x_in,
                         y_in,
                         stride_x,
                         stride_y,
                         pad,
                         randmin,
                         randmax,
                         batches,
                         generate_bias=generate_bias,
                         out_activation_min=out_activation_min,
                         out_activation_max=out_activation_max,
                         int16xint8=int16xint8,
                         bias_min=bias_min,
                         bias_max=bias_max)

    def write_c_config_header(self) -> None:
        super().write_c_config_header()

        filename = self.config_data
        filepath = self.headers_dir + filename
        prefix = self.testdataset.upper()

        with open(filepath, "a") as f:
            f.write("#define {}_OUTPUT_MULTIPLIER {}\n".format(prefix, self.quantized_multiplier))
            f.write("#define {}_OUTPUT_SHIFT {}\n".format(prefix, self.quantized_shift))
            f.write("#define {}_ACCUMULATION_DEPTH {}\n".format(prefix, self.input_ch * self.x_input * self.y_input))
            f.write("#define {}_INPUT_OFFSET {}\n".format(prefix, -self.input_zero_point))
            f.write("#define {}_OUTPUT_OFFSET {}\n".format(prefix, self.output_zero_point))

    def quantize_multiplier(self):
        input_product_scale = self.input_scale * self.weights_scale
        if input_product_scale < 0:
            raise RuntimeError("negative input product scale")
        real_multipler = input_product_scale / self.output_scale
        (self.quantized_multiplier, self.quantized_shift) = self.quantize_scale(real_multipler)

    def generate_data(self, input_data=None, weights=None, biases=None) -> None:
        input_data = self.get_randomized_input_data(input_data,
                                                    [self.batches, self.input_ch * self.x_input * self.y_input])

        if self.is_int16xint8:
            inttype = tf.int16
            datatype = "int16_t"
            bias_datatype = "int64_t"
        else:
            inttype = tf.int8
            datatype = "int8_t"
            bias_datatype = "int32_t"

        fc_weights_format = [self.input_ch * self.y_input * self.x_input, self.output_ch]

        if weights is not None:
            weights = tf.reshape(weights, fc_weights_format)
        else:
            weights = self.get_randomized_data(fc_weights_format,
                                               self.kernel_table_file,
                                               minrange=INT32_MIN,
                                               maxrange=INT32_MAX,
                                               regenerate=self.regenerate_new_weights)

        biases = self.get_randomized_bias_data(biases)

        # Create model with one fully_connected layer.
        model = tf.keras.models.Sequential()
        model.add(
            tf.keras.layers.InputLayer(input_shape=(self.y_input * self.x_input * self.input_ch, ),
                                       batch_size=self.batches))
        fully_connected_layer = tf.keras.layers.Dense(self.output_ch, activation=None)
        model.add(fully_connected_layer)
        fully_connected_layer.set_weights([weights, biases])

        interpreter = self.convert_and_interpret(model, inttype, input_data)

        all_layers_details = interpreter.get_tensor_details()
        if self.generate_bias:
            filter_layer = all_layers_details[2]
            bias_layer = all_layers_details[1]
        else:
            filter_layer = all_layers_details[1]
        if weights.numpy().size != interpreter.get_tensor(filter_layer['index']).size or \
           (self.generate_bias and biases.numpy().size != interpreter.get_tensor(bias_layer['index']).size):
            raise RuntimeError(f"Dimension mismatch for {self.testdataset}")

        # The generic destination size calculation for these tests are: self.x_output * self.y_output * self.output_ch
        # * self.batches.
        self.x_output = 1
        self.y_output = 1
        output_details = interpreter.get_output_details()
        if self.output_ch != output_details[0]['shape'][1] or self.batches != output_details[0]['shape'][0]:
            raise RuntimeError("Fully connected out dimension mismatch")

        self.weights_scale = filter_layer['quantization_parameters']['scales'][0]
        self.quantize_multiplier()

        self.generate_c_array(self.input_data_file_prefix, input_data, datatype=datatype)
        self.generate_c_array(self.weight_data_file_prefix, interpreter.get_tensor(filter_layer['index']))

        if self.generate_bias:
            self.generate_c_array(self.bias_data_file_prefix, interpreter.get_tensor(bias_layer['index']),
                                  bias_datatype)
        else:
            self.generate_c_array(self.bias_data_file_prefix, biases, bias_datatype)

        # Generate reference
        interpreter.invoke()
        output_data = interpreter.get_tensor(output_details[0]["index"])
        self.generate_c_array(self.output_data_file_prefix,
                              np.clip(output_data, self.out_activation_min, self.out_activation_max),
                              datatype=datatype)

        self.write_c_config_header()
        self.write_c_header_wrapper()


class SoftmaxSettings(TestSettings):
    softmax_input_integer_bits = 5

    def __init__(self,
                 dataset,
                 testtype,
                 regenerate_weights,
                 regenerate_input,
                 regenerate_biases,
                 schema_file,
                 x_in=5,
                 y_in=1,
                 randmin=INT8_MIN,
                 randmax=INT8_MAX,
                 int16xint8=False,
                 inInt8outInt16=False,
                 input_scale=0.003922,
                 input_zp=-128):
        super().__init__(dataset,
                         testtype,
                         regenerate_weights,
                         regenerate_input,
                         regenerate_biases,
                         schema_file,
                         1,
                         1,
                         x_in,
                         y_in,
                         1,
                         1,
                         1,
                         1,
                         False,
                         randmin,
                         randmax,
                         int16xint8=int16xint8)
        self.x_input = self.x_output = x_in
        self.y_input = self.y_output = y_in
        self.inInt8outInt16 = inInt8outInt16

        if self.inInt8outInt16 and self.is_int16xint8:
            raise RuntimeError("Specify input as either s8 or s16")

        if self.inInt8outInt16:
            self.input_scale = input_scale
            self.json_template = "TestCases/Common/Softmax/softmax_int8_to_int16_template.json"
            self.json_replacements = {
                "num_rows": self.y_input,
                "row_size": self.x_input,
                "input_scale": input_scale,
                "input_zp": input_zp
            }

    def calc_softmax_params(self):
        if self.is_int16xint8:
            input_scale_beta_rescale = self.input_scale / (10.0 / 65535.0)
            (self.input_multiplier, self.input_left_shift) = self.quantize_scale(input_scale_beta_rescale)
        else:
            input_real_multiplier = min(self.input_scale * (1 << (31 - self.softmax_input_integer_bits)), (1 << 31) - 1)
            (self.input_multiplier, self.input_left_shift) = self.quantize_scale(input_real_multiplier)

            self.diff_min = ((1 << self.softmax_input_integer_bits) - 1) * \
                            (1 << (31 - self.softmax_input_integer_bits)) / \
                            (1 << self.input_left_shift)
            self.diff_min = math.floor(self.diff_min)

    def write_c_config_header(self) -> None:
        super().write_c_config_header(write_common_parameters=False)

        filename = self.config_data
        filepath = self.headers_dir + filename
        prefix = self.testdataset.upper()

        with open(filepath, "a") as f:
            f.write("#define {}_NUM_ROWS {}\n".format(prefix, self.y_input))
            f.write("#define {}_ROW_SIZE {}\n".format(prefix, self.x_input))
            f.write("#define {}_INPUT_MULT {}\n".format(prefix, self.input_multiplier))
            f.write("#define {}_INPUT_LEFT_SHIFT {}\n".format(prefix, self.input_left_shift))
            if not self.is_int16xint8:
                f.write("#define {}_DIFF_MIN {}\n".format(prefix, -self.diff_min))
            f.write("#define {}_DST_SIZE {}\n".format(prefix, self.x_output * self.y_output))

    def get_softmax_randomized_input_data(self, input_data, input_shape):
        # Generate or load saved input data unless hardcoded data provided.
        if input_data is not None:
            input_data = tf.reshape(input_data, input_shape)
        else:
            input_data = self.get_randomized_data(input_shape,
                                                  self.inputs_table_file,
                                                  regenerate=self.regenerate_new_input)
        return input_data

    def generate_data(self, input_data=None, weights=None, biases=None) -> None:
        input_data = self.get_softmax_randomized_input_data(input_data, [self.y_input, self.x_input])

        if self.is_int16xint8:
            inttype = tf.int16
            datatype = "int16_t"
        else:
            inttype = tf.int8
            datatype = "int8_t"

        self.generate_c_array(self.input_data_file_prefix, input_data, datatype=datatype)

        # Generate reference.
        if self.inInt8outInt16:
            # Output is int16.
            datatype = "int16_t"

            # Keras does not support int8 input and int16 output for Softmax.
            # Using a template json instead.
            generated_json = self.generate_json_from_template()
            self.flatc_generate_tflite(generated_json, self.schema_file)

            interpreter = Interpreter(model_path=str(self.model_path_tflite),
                                      experimental_op_resolver_type=OpResolverType.BUILTIN_REF)
            interpreter.allocate_tensors()
            all_layers_details = interpreter.get_tensor_details()
            input_layer = all_layers_details[0]
            output_layer = all_layers_details[1]

            interpreter.set_tensor(input_layer["index"], tf.cast(input_data, tf.int8))
            interpreter.invoke()
            output_data = interpreter.get_tensor(output_layer["index"])
        else:
            # Create a one-layer Keras model.
            model = tf.keras.models.Sequential()
            input_shape = (self.y_input, self.x_input)
            model.add(tf.keras.layers.Softmax(input_shape=input_shape))

            interpreter = self.convert_and_interpret(model, inttype, tf.expand_dims(input_data, axis=0))
            output_details = interpreter.get_output_details()
            interpreter.invoke()
            output_data = interpreter.get_tensor(output_details[0]["index"])

        self.calc_softmax_params()
        self.generate_c_array(self.output_data_file_prefix, output_data, datatype=datatype)

        self.write_c_config_header()
        self.write_c_header_wrapper()


class SVDFSettings(TestSettings):

    def __init__(self,
                 dataset,
                 testtype,
                 regenerate_weights,
                 regenerate_input,
                 regenerate_biases,
                 schema_file,
                 batches=2,
                 number_inputs=2,
                 rank=8,
                 memory_size=10,
                 randmin=INT8_MIN,
                 randmax=INT8_MAX,
                 input_size=3,
                 number_units=4,
                 generate_bias=True,
                 input_scale=0.1,
                 input_zp=0,
                 w_1_scale=0.005,
                 w_1_zp=0,
                 w_2_scale=0.005,
                 w_2_zp=0,
                 bias_scale=0.000001,
                 bias_zp=0,
                 state_scale=0.005,
                 state_zp=0,
                 output_scale=0.1,
                 output_zp=0):
        super().__init__(dataset,
                         testtype,
                         regenerate_weights,
                         regenerate_input,
                         regenerate_biases,
                         schema_file,
                         1,
                         1,
                         1,
                         1,
                         1,
                         1,
                         1,
                         1,
                         False,
                         randmin,
                         randmax,
                         generate_bias=generate_bias)
        self.batches = batches
        self.number_units = number_units
        self.input_size = input_size
        self.memory_size = memory_size
        self.rank = rank
        self.number_filters = self.number_units * self.rank
        self.time_table_file = self.pregenerated_data_dir + self.testdataset + '/' + 'time_data.txt'

        self.number_inputs = number_inputs
        self.input_sequence_length = self.number_inputs * self.input_size * self.batches

        self.in_activation_max = INT16_MAX
        self.in_activation_min = INT16_MIN

        self.json_template = "TestCases/Common/svdf_template.json"
        self.json_replacements = {
            "memory_sizeXnumber_filters": self.memory_size * self.number_filters,
            "batches": self.batches,
            "input_size": self.input_size,
            "number_filters": self.number_filters,
            "memory_size": self.memory_size,
            "number_units": self.number_units,
            "rank_value": self.rank,
            "input_scale": input_scale,
            "input_zp": input_zp,
            "w_1_scale": w_1_scale,
            "w_1_zp": w_1_zp,
            "w_2_scale": w_2_scale,
            "w_2_zp": w_2_zp,
            "bias_scale": bias_scale,
            "bias_zp": bias_zp,
            "state_scale": state_scale,
            "state_zp": state_zp,
            "output_scale": output_scale,
            "output_zp": output_zp
        }

    def calc_multipliers_and_shifts(self, input_scale, weights_1_scale, weights_2_scale, state_scale, output_scale):
        effective_scale_1 = weights_1_scale * input_scale / state_scale
        effective_scale_2 = state_scale * weights_2_scale / output_scale
        (self.multiplier_in, self.shift_1) = self.quantize_scale(effective_scale_1)
        (self.multiplier_out, self.shift_2) = self.quantize_scale(effective_scale_2)

    def write_c_config_header(self) -> None:
        super().write_c_config_header(write_common_parameters=False)

        filename = self.config_data
        filepath = self.headers_dir + filename
        prefix = self.testdataset.upper()

        with open(filepath, "a") as f:
            f.write("#define {}_MULTIPLIER_IN {}\n".format(prefix, self.multiplier_in))
            f.write("#define {}_MULTIPLIER_OUT {}\n".format(prefix, self.multiplier_out))
            f.write("#define {}_SHIFT_1 {}\n".format(prefix, self.shift_1))
            f.write("#define {}_SHIFT_2 {}\n".format(prefix, self.shift_2))
            f.write("#define {}_IN_ACTIVATION_MIN {}\n".format(prefix, self.in_activation_min))
            f.write("#define {}_IN_ACTIVATION_MAX {}\n".format(prefix, self.in_activation_max))
            f.write("#define {}_RANK {}\n".format(prefix, self.rank))
            f.write("#define {}_FEATURE_BATCHES {}\n".format(prefix, self.number_filters))
            f.write("#define {}_TIME_BATCHES {}\n".format(prefix, self.memory_size))
            f.write("#define {}_INPUT_SIZE {}\n".format(prefix, self.input_size))
            f.write("#define {}_DST_SIZE {}\n".format(prefix, self.number_units * self.batches))
            f.write("#define {}_OUT_ACTIVATION_MIN {}\n".format(prefix, self.out_activation_min))
            f.write("#define {}_OUT_ACTIVATION_MAX {}\n".format(prefix, self.out_activation_max))
            f.write("#define {}_INPUT_BATCHES {}\n".format(prefix, self.batches))
            f.write("#define {}_INPUT_OFFSET {}\n".format(prefix, self.input_zero_point))
            f.write("#define {}_OUTPUT_OFFSET {}\n".format(prefix, self.output_zero_point))

    def generate_data(self, input_data=None, weights=None, biases=None, time_data=None, state_data=None) -> None:
        if input_data is not None:
            input_data = tf.reshape(input_data, [self.input_sequence_length])
        else:
            input_data = self.get_randomized_data([self.input_sequence_length],
                                                  self.inputs_table_file,
                                                  regenerate=self.regenerate_new_input)
        self.generate_c_array("input_sequence", input_data)

        if weights is not None:
            weights_feature_data = tf.reshape(weights, [self.number_filters, self.input_size])
        else:
            weights_feature_data = self.get_randomized_data([self.number_filters, self.input_size],
                                                            self.kernel_table_file,
                                                            regenerate=self.regenerate_new_weights)

        if time_data is not None:
            weights_time_data = tf.reshape(time_data, [self.number_filters, self.memory_size])
        else:
            weights_time_data = self.get_randomized_data([self.number_filters, self.memory_size],
                                                         self.time_table_file,
                                                         regenerate=self.regenerate_new_weights)

        if not self.generate_bias:
            biases = [0] * self.number_units
        if biases is not None:
            biases = tf.reshape(biases, [self.number_units])
        else:
            biases = self.get_randomized_data([self.number_units],
                                              self.bias_table_file,
                                              regenerate=self.regenerate_new_weights)

        # Generate tflite model
        generated_json = self.generate_json_from_template(weights_feature_data, weights_time_data, biases)
        self.flatc_generate_tflite(generated_json, self.schema_file)

        # Run TFL interpreter
        interpreter = Interpreter(model_path=str(self.model_path_tflite),
                                  experimental_op_resolver_type=OpResolverType.BUILTIN_REF)
        interpreter.allocate_tensors()

        # Read back scales and zero points from tflite model
        all_layers_details = interpreter.get_tensor_details()
        input_layer = all_layers_details[0]
        weights_1_layer = all_layers_details[1]
        weights_2_layer = all_layers_details[2]
        bias_layer = all_layers_details[3]
        state_layer = all_layers_details[4]
        output_layer = all_layers_details[5]
        (input_scale, self.input_zero_point) = self.get_scale_and_zp(input_layer)
        (weights_1_scale, zero_point) = self.get_scale_and_zp(weights_1_layer)
        (weights_2_scale, zero_point) = self.get_scale_and_zp(weights_2_layer)
        (bias_scale, zero_point) = self.get_scale_and_zp(bias_layer)
        (state_scale, zero_point) = self.get_scale_and_zp(state_layer)
        (output_scale, self.output_zero_point) = self.get_scale_and_zp(output_layer)

        self.calc_multipliers_and_shifts(input_scale, weights_1_scale, weights_2_scale, state_scale, output_scale)

        # Generate unit test C headers
        self.generate_c_array("weights_feature", interpreter.get_tensor(weights_1_layer['index']))
        self.generate_c_array("weights_time", interpreter.get_tensor(weights_2_layer['index']), datatype='int16_t')
        self.generate_c_array(self.bias_data_file_prefix, interpreter.get_tensor(bias_layer['index']), "int32_t")
        self.generate_c_array("state", interpreter.get_tensor(state_layer['index']), "int16_t")

        # Generate reference output
        svdf_ref = None
        for i in range(self.number_inputs):
            start = i * self.input_size * self.batches
            end = i * self.input_size * self.batches + self.input_size * self.batches
            input_sequence = input_data[start:end]
            input_sequence = tf.reshape(input_sequence, [self.batches, self.input_size])
            interpreter.set_tensor(input_layer["index"], tf.cast(input_sequence, tf.int8))
            interpreter.invoke()
            svdf_ref = interpreter.get_tensor(output_layer["index"])
        self.generate_c_array(self.output_data_file_prefix, svdf_ref)

        self.write_c_config_header()
        self.write_c_header_wrapper()

    def get_scale_and_zp(self, layer):
        return (layer['quantization_parameters']['scales'][0], layer['quantization_parameters']['zero_points'][0])


class AddMulSettings(TestSettings):

    def __init__(self,
                 dataset,
                 testtype,
                 regenerate_weights,
                 regenerate_input,
                 regenerate_biases,
                 schema_file,
                 channels=1,
                 x_in=4,
                 y_in=4,
                 decimal_input=6,
                 randmin=INT8_MIN,
                 randmax=INT8_MAX,
                 out_activation_min=INT8_MIN,
                 out_activation_max=INT8_MAX,
                 int16xint8=False):
        super().__init__(dataset,
                         testtype,
                         regenerate_weights,
                         regenerate_input,
                         regenerate_biases,
                         schema_file,
                         in_ch=channels,
                         out_ch=channels,
                         x_in=x_in,
                         y_in=y_in,
                         w_x=1,
                         w_y=1,
                         stride_x=1,
                         stride_y=1,
                         pad=False,
                         randmin=randmin,
                         randmax=randmax,
                         batches=1,
                         generate_bias=False,
                         relu6=False,
                         out_activation_min=out_activation_min,
                         out_activation_max=out_activation_max,
                         int16xint8=int16xint8)

        self.x_input = self.x_output = x_in
        self.y_input = self.y_output = y_in
        self.decimal_input = decimal_input

        self.left_shift = 15 if self.is_int16xint8 else 20

    def generate_data(self, input_data1=None, input_data2=None) -> None:
        input_shape = (1, self.y_input, self.x_input, self.input_ch)

        input_data1 = self.get_randomized_data(list(input_shape),
                                               self.inputs_table_file,
                                               regenerate=self.regenerate_new_input,
                                               decimals=self.decimal_input)
        input_data2 = self.get_randomized_data(list(input_shape),
                                               self.kernel_table_file,
                                               regenerate=self.regenerate_new_weights,
                                               decimals=self.decimal_input)

        if self.is_int16xint8:
            inttype = "int16_t"
            inttype_tf = tf.int16
        else:
            inttype = "int8_t"
            inttype_tf = tf.int8

        # Create a one-layer functional Keras model as add/mul cannot use a sequntial Keras model.
        input1 = tf.keras.layers.Input(shape=input_shape[1:])
        input2 = tf.keras.layers.Input(shape=input_shape[1:])
        if self.test_type == 'add':
            layer = tf.keras.layers.Add()([input1, input2])
        elif self.test_type == 'mul':
            layer = tf.keras.layers.Multiply()([input1, input2])
        else:
            raise RuntimeError("Wrong test type")
        out = tf.keras.layers.Lambda(function=lambda x: x)(layer)
        model = tf.keras.models.Model(inputs=[input1, input2], outputs=out)

        interpreter = self.convert_and_interpret(model, inttype_tf)

        input_details = interpreter.get_input_details()
        interpreter.set_tensor(input_details[0]["index"], tf.cast(input_data1, inttype_tf))
        interpreter.set_tensor(input_details[1]["index"], tf.cast(input_data2, inttype_tf))

        # Calculate multipliers, shifts and offsets.
        (input1_scale, self.input1_zero_point) = input_details[0]['quantization']
        (input2_scale, self.input2_zero_point) = input_details[1]['quantization']
        self.input1_zero_point = -self.input1_zero_point
        self.input2_zero_point = -self.input2_zero_point
        double_max_input_scale = max(input1_scale, input2_scale) * 2
        (self.input1_mult, self.input1_shift) = self.quantize_scale(input1_scale / double_max_input_scale)
        (self.input2_mult, self.input2_shift) = self.quantize_scale(input2_scale / double_max_input_scale)

        if self.test_type == 'add':
            actual_output_scale = double_max_input_scale / ((1 << self.left_shift) * self.output_scale)
        elif self.test_type == 'mul':
            actual_output_scale = input1_scale * input2_scale / self.output_scale
        (self.output_mult, self.output_shift) = self.quantize_scale(actual_output_scale)

        # Generate reference.
        interpreter.invoke()
        output_details = interpreter.get_output_details()
        output_data = interpreter.get_tensor(output_details[0]["index"])
        self.generate_c_array("input1", input_data1, datatype=inttype)
        self.generate_c_array("input2", input_data2, datatype=inttype)
        self.generate_c_array(self.output_data_file_prefix,
                              np.clip(output_data, self.out_activation_min, self.out_activation_max),
                              datatype=inttype)

        self.write_c_config_header()
        self.write_c_header_wrapper()

    def write_c_config_header(self) -> None:
        super().write_c_config_header(write_common_parameters=False)

        filename = self.config_data
        filepath = self.headers_dir + filename
        prefix = self.testdataset.upper()

        with open(filepath, "a") as f:
            f.write("#define {}_DST_SIZE {}\n".format(prefix,
                                                      self.batches * self.y_input * self.x_input * self.input_ch))
            f.write("#define {}_OUT_ACTIVATION_MIN {}\n".format(prefix, self.out_activation_min))
            f.write("#define {}_OUT_ACTIVATION_MAX {}\n".format(prefix, self.out_activation_max))
            f.write("#define {}_INPUT1_OFFSET {}\n".format(prefix, self.input1_zero_point))
            f.write("#define {}_INPUT2_OFFSET {}\n".format(prefix, self.input2_zero_point))
            f.write("#define {}_OUTPUT_MULT {}\n".format(prefix, self.output_mult))
            f.write("#define {}_OUTPUT_SHIFT {}\n".format(prefix, self.output_shift))
            f.write("#define {}_OUTPUT_OFFSET {}\n".format(prefix, self.output_zero_point))
            if self.test_type == 'add':
                f.write("#define {}_LEFT_SHIFT {}\n".format(prefix, self.left_shift))
                f.write("#define {}_INPUT1_SHIFT {}\n".format(prefix, self.input1_shift))
                f.write("#define {}_INPUT2_SHIFT {}\n".format(prefix, self.input2_shift))
                f.write("#define {}_INPUT1_MULT {}\n".format(prefix, self.input1_mult))
                f.write("#define {}_INPUT2_MULT {}\n".format(prefix, self.input2_mult))


class LSTMSettings(TestSettings):

    def __init__(self,
                 dataset,
                 testtype,
                 regenerate_weights,
                 regenerate_input,
                 regenerate_biases,
                 schema_file,
                 batches=2,
                 time_steps=2,
                 number_inputs=3,
                 number_units=4,
                 time_major=True,
                 randmin=INT8_MIN,
                 randmax=INT8_MAX,
                 generate_bias=True):
        super().__init__(dataset,
                         testtype,
                         regenerate_weights,
                         regenerate_input,
                         regenerate_biases,
                         schema_file,
                         1,
                         1,
                         1,
                         1,
                         1,
                         1,
                         1,
                         1,
                         False,
                         randmin,
                         randmax,
                         generate_bias=generate_bias)

        self.batches = batches
        self.time_steps = time_steps
        self.number_units = number_units
        self.number_inputs = number_inputs

        self.kernel_hidden_table_file = self.pregenerated_data_dir + self.testdataset + '/' + 'kernel_hidden.txt'

        self.time_major = time_major

        self.in_activation_max = INT16_MAX
        self.in_activation_min = INT16_MIN

        self.lstm_scales = []

        # Layer indexes. Works with tensorflow 2.10 and 2.11.
        self.output_gate_bias_index = 1
        self.cell_gate_bias_index = 2
        self.forget_gate_bias_index = 3
        self.input_gate_bias_index = 4
        self.recurrent_input_to_output_w_index = 5
        self.recurrent_input_to_cell_w_index = 6
        self.recurrent_input_to_forget_w_index = 7
        self.recurrent_input_to_input_w_index = 8
        self.input_to_output_w_index = 9
        self.input_to_cell_w_index = 10
        self.input_to_forget_w_index = 11
        self.input_to_input_w_index = 12
        self.output_state_index = 13
        self.cell_state_index = 14
        self.input_norm_coeff_index = 15
        self.forget_norm_coeff_index = 16
        self.cell_norm_coeff_index = 17
        self.output_norm_coeff_index = 18
        self.effective_hidden_scale_intermediate_index = 20

    def generate_data(self, input_data=None, weights=None, hidden_weights=None, biases=None) -> None:

        input_dims = [self.batches, self.time_steps, self.number_inputs]
        if input_data is not None:
            input_data = tf.reshape(input_data, input_dims)
        else:
            input_data = self.get_randomized_data(input_dims,
                                                  self.inputs_table_file,
                                                  regenerate=self.regenerate_new_input)

        # This will be the same size when there is no projection.
        number_cells = self.number_units

        # Each LSTM cell has 4 input weights, 4 hidden (recurrent or cell state) weights and 4 biases.
        number_w_b = 4

        if weights is not None:
            weights = tf.reshape(weights, [self.number_inputs, number_cells * number_w_b])
        else:
            weights = self.get_randomized_data([self.number_inputs, number_cells * number_w_b],
                                               self.kernel_table_file,
                                               regenerate=self.regenerate_new_weights,
                                               decimals=8,
                                               minrange=-1.0,
                                               maxrange=1.0)

        if hidden_weights is not None:
            hidden_weights = tf.reshape(hidden_weights, [number_cells, number_cells * number_w_b])
        else:
            hidden_weights = self.get_randomized_data([number_cells, number_cells * number_w_b],
                                                      self.kernel_hidden_table_file,
                                                      regenerate=self.regenerate_new_weights,
                                                      decimals=8,
                                                      minrange=-1.0,
                                                      maxrange=1.0)
        if not self.generate_bias:
            biases = [0] * number_cells * number_w_b
        if biases is not None:
            biases = tf.reshape(biases, [number_cells * number_w_b])
        else:
            biases = self.get_randomized_data([number_cells * number_w_b],
                                              self.bias_table_file,
                                              regenerate=self.regenerate_new_bias,
                                              decimals=8,
                                              minrange=-1.0,
                                              maxrange=1.0)

        # Create a Keras based LSTM model.
        input_layer = tf.keras.layers.Input(shape=(self.time_steps, self.number_inputs),
                                            batch_size=self.batches,
                                            name='input')
        if self.time_major:
            input_layer_transposed = tf.transpose(input_layer, perm=[1, 0, 2])
            lstm_layer = tf.keras.layers.LSTM(units=self.number_units,
                                              time_major=self.time_major,
                                              return_sequences=True)(input_layer_transposed)
        else:
            lstm_layer = tf.keras.layers.LSTM(units=self.number_units,
                                              time_major=self.time_major,
                                              return_sequences=True)(input_layer)
        model = tf.keras.Model(input_layer, lstm_layer, name="LSTM")

        if self.time_major:
            time_major_offset = 1
            shape = (self.time_steps, self.batches, self.number_inputs)
        else:
            time_major_offset = 0
            shape = (self.batches, self.time_steps, self.number_inputs)

        # Writing weight and bias to model.
        print("Updating weights", model.layers[1 + time_major_offset].weights[0].name)
        model.layers[1 + time_major_offset].weights[0].assign(weights)
        print("Updating hidden weights", model.layers[1 + time_major_offset].weights[1].name)
        model.layers[1 + time_major_offset].weights[1].assign(hidden_weights)
        print("Updating bias", model.layers[1 + time_major_offset].weights[2].name)
        model.layers[1 + time_major_offset].weights[2].assign(biases)

        interpreter = self.convert_and_interpret(model, tf.int8, input_data, dataset_shape=shape)

        all_layers_details = interpreter.get_tensor_details()

        for i in all_layers_details:
            self.lstm_scales.append(i['quantization_parameters']['scales'])

        input_data_for_index = all_layers_details[0]

        input_gate_bias = all_layers_details[self.input_gate_bias_index + time_major_offset]
        forget_gate_bias = all_layers_details[self.forget_gate_bias_index + time_major_offset]
        cell_gate_bias = all_layers_details[self.cell_gate_bias_index + time_major_offset]
        output_gate_bias = all_layers_details[self.output_gate_bias_index + time_major_offset]

        input_to_input_w = all_layers_details[self.input_to_input_w_index + time_major_offset]
        input_to_forget_w = all_layers_details[self.input_to_forget_w_index + time_major_offset]
        input_to_cell_w = all_layers_details[self.input_to_cell_w_index + time_major_offset]
        input_to_output_w = all_layers_details[self.input_to_output_w_index + time_major_offset]

        recurrent_input_to_input_w = all_layers_details[self.recurrent_input_to_input_w_index + time_major_offset]
        recurrent_input_to_forget_w = all_layers_details[self.recurrent_input_to_forget_w_index + time_major_offset]
        recurrent_input_to_cell_w = all_layers_details[self.recurrent_input_to_cell_w_index + time_major_offset]
        recurrent_input_to_output_w = all_layers_details[self.recurrent_input_to_output_w_index + time_major_offset]

        if self.time_major:
            time_major_offset = 2

        output_state = all_layers_details[self.output_state_index + time_major_offset]
        cell_state = all_layers_details[self.cell_state_index + time_major_offset]

        input_norm_coeff = all_layers_details[self.input_norm_coeff_index + time_major_offset]
        forget_norm_coeff = all_layers_details[self.forget_norm_coeff_index + time_major_offset]
        cell_norm_coeff = all_layers_details[self.cell_norm_coeff_index + time_major_offset]
        output_norm_coeff = all_layers_details[self.output_norm_coeff_index + time_major_offset]

        # For scale and zero point.
        effective_hidden_scale_intermediate = all_layers_details[self.effective_hidden_scale_intermediate_index +
                                                                 time_major_offset]

        input_details = interpreter.get_input_details()
        output_details = interpreter.get_output_details()
        actual_input_data = interpreter.get_tensor(input_details[0]["index"])
        if (input_data.numpy().shape != actual_input_data.shape) or \
           not ((input_data.numpy().astype(int) == actual_input_data).all().astype(int)):
            raise RuntimeError("Input data mismatch")

        self.generate_c_array(self.input_data_file_prefix, interpreter.get_tensor(input_data_for_index['index']))
        self.generate_c_array("input_to_input_w", interpreter.get_tensor(input_to_input_w['index']))
        self.generate_c_array("input_to_forget_w", interpreter.get_tensor(input_to_forget_w['index']))
        self.generate_c_array("input_to_cell_w", interpreter.get_tensor(input_to_cell_w['index']))
        self.generate_c_array("input_to_output_w", interpreter.get_tensor(input_to_output_w['index']))
        self.generate_c_array("recurrent_input_to_input_w", interpreter.get_tensor(recurrent_input_to_input_w['index']))
        self.generate_c_array("recurrent_input_to_forget_w",
                              interpreter.get_tensor(recurrent_input_to_forget_w['index']))
        self.generate_c_array("recurrent_input_to_cell_w", interpreter.get_tensor(recurrent_input_to_cell_w['index']))
        self.generate_c_array("recurrent_input_to_output_w",
                              interpreter.get_tensor(recurrent_input_to_output_w['index']))

        # Peephole not supported so these are nullptrs.
        self.generate_c_array("cell_to_input", [], datatype='int16_t')
        self.generate_c_array("cell_to_forget", [], datatype='int16_t')
        self.generate_c_array("cell_to_output", [], datatype='int16_t')

        self.generate_c_array("input_gate_bias", interpreter.get_tensor(input_gate_bias['index']), datatype='int32_t')
        self.generate_c_array("cell_gate_bias", interpreter.get_tensor(cell_gate_bias['index']), datatype='int32_t')
        self.generate_c_array("forget_gate_bias", interpreter.get_tensor(forget_gate_bias['index']), datatype='int32_t')
        self.generate_c_array("output_gate_bias", interpreter.get_tensor(output_gate_bias['index']), datatype='int32_t')

        # Projection not supported so these are nullptrs.
        self.generate_c_array("projection_weights", [])
        self.generate_c_array("projection_bias", [], datatype='int32_t')

        self.generate_c_array("output_state", interpreter.get_tensor(output_state['index']), const="")
        self.generate_c_array("cell_state", interpreter.get_tensor(cell_state['index']), datatype='int16_t', const="")

        self.generate_c_array("input_norm_coeff", interpreter.get_tensor(input_norm_coeff['index']))
        self.generate_c_array("forget_norm_coeff", interpreter.get_tensor(forget_norm_coeff['index']))
        self.generate_c_array("cell_norm_coeff", interpreter.get_tensor(cell_norm_coeff['index']))
        self.generate_c_array("output_norm_coeff", interpreter.get_tensor(output_norm_coeff['index']))

        input_scale = input_data_for_index['quantization_parameters']['scales'][0]
        cell_scale = cell_state['quantization_parameters']['scales'][0]
        output_state_scale = output_state['quantization_parameters']['scales'][0]
        input_zp = input_data_for_index['quantization_parameters']['zero_points'][0]
        output_zp = output_details[0]['quantization_parameters']['zero_points'][0]
        output_state_zp = output_state['quantization_parameters']['zero_points'][0]
        self.hidden_zp = effective_hidden_scale_intermediate['quantization_parameters']['zero_points'][0]
        self.output_state_offset = output_state_zp

        tmp = math.log(cell_scale) * (1 / math.log(2))
        self.cell_state_shift = int(round(tmp))

        self.calc_scales(input_scale, output_state_scale)

        # Calculate effective biases.
        input_zp = -input_zp
        output_zp = -output_zp
        output_state_zp = -output_state_zp
        input_to_forget_eff_bias = self.calc_effective_bias(interpreter, input_zp, input_to_forget_w, forget_gate_bias)
        recurrent_to_forget_eff_bias = self.calc_effective_bias(interpreter, output_state_zp,
                                                                recurrent_input_to_forget_w, None, False)
        input_to_cell_eff_bias = self.calc_effective_bias(interpreter, input_zp, input_to_cell_w, cell_gate_bias)
        recurrent_to_cell_eff_bias = self.calc_effective_bias(interpreter, output_state_zp, recurrent_input_to_cell_w,
                                                              None, False)
        input_to_output_eff_bias = self.calc_effective_bias(interpreter, input_zp, input_to_output_w, output_gate_bias)
        recurrent_to_output_eff_bias = self.calc_effective_bias(interpreter, output_state_zp,
                                                                recurrent_input_to_output_w, None, False)
        input_to_input_eff_bias = self.calc_effective_bias(interpreter, input_zp, input_to_input_w, input_gate_bias)

        recurrent_to_input_eff_bias = self.calc_effective_bias(interpreter, output_state_zp, recurrent_input_to_input_w,
                                                               None, False)

        self.generate_c_array("input_to_input_eff_bias", input_to_input_eff_bias, datatype='int32_t')
        self.generate_c_array("input_to_forget_eff_bias", input_to_forget_eff_bias, datatype='int32_t')
        self.generate_c_array("input_to_cell_eff_bias", input_to_cell_eff_bias, datatype='int32_t')
        self.generate_c_array("input_to_output_eff_bias", input_to_output_eff_bias, datatype='int32_t')
        self.generate_c_array("recurrent_to_input_eff_bias", recurrent_to_input_eff_bias, datatype='int32_t')
        self.generate_c_array("recurrent_to_cell_eff_bias", recurrent_to_cell_eff_bias, datatype='int32_t')
        self.generate_c_array("recurrent_to_forget_eff_bias", recurrent_to_forget_eff_bias, datatype='int32_t')
        self.generate_c_array("recurrent_to_output_eff_bias", recurrent_to_output_eff_bias, datatype='int32_t')

        # Generate reference
        interpreter.invoke()
        output_data = interpreter.get_tensor(output_details[0]["index"])
        self.generate_c_array(self.output_data_file_prefix, output_data, datatype='int8_t')

        self.write_c_config_header()
        self.write_c_header_wrapper()

    def calc_scales(self, input_scale, output_state_scale):
        intermediate_scale = pow(2, -12)

        if self.time_major:
            time_major_offset = 1
        else:
            time_major_offset = 0

        self.effective_hidden_scale = pow(2, -15) / output_state_scale * pow(2, -15)

        self.i2i_effective_scale = input_scale * self.lstm_scales[self.input_to_input_w_index + time_major_offset][0] \
            / intermediate_scale
        self.i2f_effective_scale = input_scale * self.lstm_scales[self.input_to_forget_w_index + time_major_offset][0] \
            / intermediate_scale
        self.i2c_effective_scale = input_scale * self.lstm_scales[self.input_to_cell_w_index + time_major_offset][0] \
            / intermediate_scale
        self.i2o_effective_scale = input_scale * self.lstm_scales[self.input_to_output_w_index + time_major_offset][0] \
            / intermediate_scale

        self.r2i_effective_scale = output_state_scale * self.lstm_scales[self.recurrent_input_to_input_w_index +
                                                                         time_major_offset][0] / intermediate_scale
        self.r2f_effective_scale = output_state_scale * self.lstm_scales[self.recurrent_input_to_forget_w_index +
                                                                         time_major_offset][0] / intermediate_scale
        self.r2c_effective_scale = output_state_scale * self.lstm_scales[self.recurrent_input_to_cell_w_index +
                                                                         time_major_offset][0] / intermediate_scale
        self.r2o_effective_scale = output_state_scale * self.lstm_scales[self.recurrent_input_to_output_w_index +
                                                                         time_major_offset][0] / intermediate_scale

    def calc_effective_bias(self, interpreter, zero_point, weight_tensor, bias_tensor, has_bias=True) -> list:

        weights = interpreter.get_tensor(weight_tensor['index'])
        dims = weight_tensor['shape']
        row = dims[0]
        col = dims[1]

        if has_bias:
            bias_data = interpreter.get_tensor(bias_tensor['index'])
            output = bias_data
        else:
            output = np.zeros((row, ), dtype=np.int32)

        for i_row in range(row):
            row_sum = 0
            for i_col in range(col):
                row_sum = row_sum + weights[i_row][i_col]
            output[i_row] = output[i_row] + row_sum * zero_point

        return output

    def write_c_config_header(self) -> None:
        super().write_c_config_header(write_common_parameters=False)

        filename = self.config_data
        filepath = self.headers_dir + filename
        prefix = self.testdataset.upper()

        with open(filepath, "a") as f:
            f.write("#define {}_BUFFER_SIZE {}\n".format(prefix, self.batches * self.number_units))
            f.write("#define {}_INPUT_BATCHES {}\n".format(prefix, self.batches))
            f.write("#define {}_DST_SIZE {}\n".format(prefix, self.batches * self.time_steps * self.number_units))
            f.write("#define {}_TIME_STEPS {}\n".format(prefix, self.time_steps))
            f.write("#define {}_NUMBER_UNITS {}\n".format(prefix, self.number_units))
            f.write("#define {}_NUMBER_INPUTS {}\n".format(prefix, self.number_inputs))
            f.write("#define {}_TIME_MAJOR {}\n".format(prefix, int(self.time_major)))
            f.write("#define {}_IN_ACTIVATION_MIN {}\n".format(prefix, self.in_activation_min))
            f.write("#define {}_IN_ACTIVATION_MAX {}\n".format(prefix, self.in_activation_max))

            (multiplier, shift) = self.quantize_scale(self.i2i_effective_scale)
            f.write("#define {}_IN_TO_INPUT_MULTIPLIER {}\n".format(prefix, multiplier))
            f.write("#define {}_IN_TO_INPUT_SHIFT {}\n".format(prefix, shift))
            (multiplier, shift) = self.quantize_scale(self.i2f_effective_scale)
            f.write("#define {}_IN_TO_FORGET_MULTIPLIER {}\n".format(prefix, multiplier))
            f.write("#define {}_IN_TO_FORGET_SHIFT {}\n".format(prefix, shift))
            (multiplier, shift) = self.quantize_scale(self.i2c_effective_scale)
            f.write("#define {}_IN_TO_CELL_MULTIPLIER {}\n".format(prefix, multiplier))
            f.write("#define {}_IN_TO_CELL_SHIFT {}\n".format(prefix, shift))
            (multiplier, shift) = self.quantize_scale(self.i2o_effective_scale)
            f.write("#define {}_IN_TO_OUTPUT_MULTIPLIER {}\n".format(prefix, multiplier))
            f.write("#define {}_IN_TO_OUTPUT_SHIFT {}\n".format(prefix, shift))

            (multiplier, shift) = self.quantize_scale(self.r2i_effective_scale)
            f.write("#define {}_RECURRENT_TO_INPUT_MULTIPLIER {}\n".format(prefix, multiplier))
            f.write("#define {}_RECURRENT_TO_INPUT_SHIFT {}\n".format(prefix, shift))
            (multiplier, shift) = self.quantize_scale(self.r2f_effective_scale)
            f.write("#define {}_RECURRENT_TO_FORGET_MULTIPLIER {}\n".format(prefix, multiplier))
            f.write("#define {}_RECURRENT_TO_FORGET_SHIFT {}\n".format(prefix, shift))
            (multiplier, shift) = self.quantize_scale(self.r2c_effective_scale)
            f.write("#define {}_RECURRENT_TO_CELL_MULTIPLIER {}\n".format(prefix, multiplier))
            f.write("#define {}_RECURRENT_TO_CELL_SHIFT {}\n".format(prefix, shift))
            (multiplier, shift) = self.quantize_scale(self.r2o_effective_scale)
            f.write("#define {}_RECURRENT_TO_OUTPUT_MULTIPLIER {}\n".format(prefix, multiplier))
            f.write("#define {}_RECURRENT_TO_OUTPUT_SHIFT {}\n".format(prefix, shift))

            (multiplier, shift) = self.quantize_scale(self.effective_hidden_scale)
            f.write("#define {}_HIDDEN_MULTIPLIER {}\n".format(prefix, multiplier))
            f.write("#define {}_HIDDEN_SHIFT {}\n".format(prefix, shift))

            f.write("#define {}_HIDDEN_OFFSET {}\n".format(prefix, self.hidden_zp))

            f.write("#define {}_OUTPUT_STATE_OFFSET {}\n".format(prefix, self.output_state_offset))
            f.write("#define {}_CELL_STATE_SHIFT {}\n".format(prefix, self.cell_state_shift))

            for i in range(len(self.lstm_scales)):
                if len(self.lstm_scales[i]) == 0:
                    continue
                (multiplier, shift) = self.quantize_scale(self.lstm_scales[i][0])


def load_testdata_sets() -> dict:
    """
    Add all new testdata sets here
    """
    testdata_sets = {}

    regenerate_input = args.regenerate_input
    regenerate_weights = args.regenerate_weights
    regenerate_biases = args.regenerate_biases

    if args.regenerate_all:
        regenerate_biases = True
        regenerate_weights = True
        regenerate_input = True

    schema_file = args.schema_file

    type_of_test = 'conv'
    dataset = 'basic'
    testdata_sets[dataset] = ConvSettings(dataset,
                                          type_of_test,
                                          regenerate_weights,
                                          regenerate_input,
                                          regenerate_biases,
                                          schema_file,
                                          in_ch=1,
                                          out_ch=1,
                                          x_in=5,
                                          y_in=8,
                                          w_x=2,
                                          w_y=4,
                                          stride_x=1,
                                          stride_y=1,
                                          pad=False)
    dataset = 'stride2pad1'
    testdata_sets[dataset] = ConvSettings(dataset,
                                          type_of_test,
                                          regenerate_weights,
                                          regenerate_input,
                                          regenerate_biases,
                                          schema_file,
                                          in_ch=1,
                                          out_ch=1,
                                          x_in=7,
                                          y_in=7,
                                          w_x=3,
                                          w_y=3,
                                          stride_x=2,
                                          stride_y=2,
                                          pad=True)
    dataset = 'kernel1x1'
    testdata_sets[dataset] = ConvSettings(dataset,
                                          type_of_test,
                                          regenerate_weights,
                                          regenerate_input,
                                          regenerate_biases,
                                          schema_file,
                                          in_ch=19,
                                          out_ch=7,
                                          x_in=7,
                                          y_in=5,
                                          w_x=1,
                                          w_y=1,
                                          stride_x=1,
                                          stride_y=1,
                                          pad=False,
                                          bias_min=INT8_MIN,
                                          bias_max=INT8_MAX,
                                          out_activation_min=-126,
                                          out_activation_max=127,
                                          batches=2)
    dataset = 'kernel1x1_stride_x'
    testdata_sets[dataset] = ConvSettings(dataset,
                                          type_of_test,
                                          regenerate_weights,
                                          regenerate_input,
                                          regenerate_biases,
                                          schema_file,
                                          in_ch=9,
                                          out_ch=5,
                                          x_in=7,
                                          y_in=4,
                                          w_x=1,
                                          w_y=1,
                                          stride_x=3,
                                          stride_y=1,
                                          pad=False,
                                          out_activation_min=-126,
                                          out_activation_max=127,
                                          batches=2)
    dataset = 'kernel1x1_stride_x_y'
    testdata_sets[dataset] = ConvSettings(dataset,
                                          type_of_test,
                                          regenerate_weights,
                                          regenerate_input,
                                          regenerate_biases,
                                          schema_file,
                                          in_ch=23,
                                          out_ch=15,
                                          randmin=0,
                                          x_in=7,
                                          y_in=6,
                                          w_x=1,
                                          w_y=1,
                                          stride_x=2,
                                          stride_y=2,
                                          pad=False,
                                          out_activation_min=-6,
                                          out_activation_max=127,
                                          batches=3)
    dataset = 'kernel1x1_stride_x_y_1'
    testdata_sets[dataset] = ConvSettings(dataset,
                                          type_of_test,
                                          regenerate_weights,
                                          regenerate_input,
                                          regenerate_biases,
                                          schema_file,
                                          in_ch=5,
                                          out_ch=5,
                                          x_in=4,
                                          y_in=4,
                                          w_x=1,
                                          w_y=1,
                                          stride_x=2,
                                          stride_y=2,
                                          pad=False,
                                          out_activation_min=-126,
                                          out_activation_max=127,
                                          batches=2)
    dataset = 'kernel1x1_stride_x_y_2'
    testdata_sets[dataset] = ConvSettings(dataset,
                                          type_of_test,
                                          regenerate_weights,
                                          regenerate_input,
                                          regenerate_biases,
                                          schema_file,
                                          in_ch=5,
                                          out_ch=5,
                                          x_in=4,
                                          y_in=4,
                                          w_x=1,
                                          w_y=1,
                                          stride_x=3,
                                          stride_y=3,
                                          pad=False,
                                          out_activation_min=-126,
                                          out_activation_max=127,
                                          batches=2)
    dataset = 'conv_3'
    testdata_sets[dataset] = ConvSettings(dataset,
                                          type_of_test,
                                          regenerate_weights,
                                          regenerate_input,
                                          regenerate_biases,
                                          schema_file,
                                          in_ch=3,
                                          out_ch=1,
                                          x_in=10,
                                          y_in=49,
                                          w_x=4,
                                          w_y=10,
                                          stride_x=1,
                                          stride_y=2,
                                          pad=True,
                                          out_activation_min=-127,
                                          out_activation_max=127)
    dataset = 'conv_1_x_n_1'  # left and right pad, no non-padded elements
    testdata_sets[dataset] = ConvSettings(dataset,
                                          type_of_test,
                                          regenerate_weights,
                                          regenerate_input,
                                          regenerate_biases,
                                          schema_file,
                                          in_ch=4,
                                          out_ch=3,
                                          x_in=2,
                                          y_in=1,
                                          w_x=3,
                                          w_y=1,
                                          stride_x=1,
                                          stride_y=1,
                                          pad=True,
                                          out_activation_min=-127,
                                          out_activation_max=127,
                                          batches=2)
    dataset = 'conv_1_x_n_2'  # no pad
    testdata_sets[dataset] = ConvSettings(dataset,
                                          type_of_test,
                                          regenerate_weights,
                                          regenerate_input,
                                          regenerate_biases,
                                          schema_file,
                                          in_ch=4,
                                          out_ch=3,
                                          x_in=296,
                                          y_in=1,
                                          w_x=48,
                                          w_y=1,
                                          stride_x=2,
                                          stride_y=1,
                                          pad=False,
                                          out_activation_min=-111,
                                          out_activation_max=127)
    dataset = 'conv_1_x_n_3'
    testdata_sets[dataset] = ConvSettings(dataset,
                                          type_of_test,
                                          regenerate_weights,
                                          regenerate_input,
                                          regenerate_biases,
                                          schema_file,
                                          in_ch=4,
                                          out_ch=1,
                                          x_in=296,
                                          y_in=1,
                                          w_x=48,
                                          w_y=1,
                                          stride_x=2,
                                          stride_y=1,
                                          pad=True,
                                          out_activation_min=-111,
                                          out_activation_max=127)
    dataset = 'conv_1_x_n_4'  # 0 left pad, 1 right pad
    testdata_sets[dataset] = ConvSettings(dataset,
                                          type_of_test,
                                          regenerate_weights,
                                          regenerate_input,
                                          regenerate_biases,
                                          schema_file,
                                          in_ch=4,
                                          out_ch=4,
                                          x_in=16,
                                          y_in=1,
                                          w_x=3,
                                          w_y=1,
                                          stride_x=2,
                                          stride_y=1,
                                          pad=True,
                                          out_activation_min=-88,
                                          out_activation_max=127)
    dataset = 'conv_1_x_n_5'
    testdata_sets[dataset] = ConvSettings(dataset,
                                          type_of_test,
                                          regenerate_weights,
                                          regenerate_input,
                                          regenerate_biases,
                                          schema_file,
                                          in_ch=4,
                                          out_ch=1,
                                          x_in=17,
                                          y_in=1,
                                          w_x=3,
                                          w_y=1,
                                          stride_x=3,
                                          stride_y=1,
                                          pad=True,
                                          out_activation_min=-88,
                                          out_activation_max=127)
    dataset = 'conv_2'
    testdata_sets[dataset] = ConvSettings(dataset,
                                          type_of_test,
                                          regenerate_weights,
                                          regenerate_input,
                                          regenerate_biases,
                                          schema_file,
                                          in_ch=2,
                                          out_ch=4,
                                          x_in=6,
                                          y_in=3,
                                          w_x=3,
                                          w_y=3,
                                          stride_x=1,
                                          stride_y=1,
                                          pad=True,
                                          out_activation_min=-101,
                                          out_activation_max=127)
    dataset = 'conv_4'  # batches > 2
    testdata_sets[dataset] = ConvSettings(dataset,
                                          type_of_test,
                                          regenerate_weights,
                                          regenerate_input,
                                          regenerate_biases,
                                          schema_file,
                                          in_ch=3,
                                          out_ch=3,
                                          x_in=5,
                                          y_in=5,
                                          w_x=2,
                                          w_y=3,
                                          stride_x=2,
                                          stride_y=2,
                                          pad=False,
                                          out_activation_min=-109,
                                          out_activation_max=127,
                                          batches=3)
    dataset = 'conv_5'
    testdata_sets[dataset] = ConvSettings(dataset,
                                          type_of_test,
                                          regenerate_weights,
                                          regenerate_input,
                                          regenerate_biases,
                                          schema_file,
                                          in_ch=128,
                                          out_ch=1,
                                          x_in=128,
                                          y_in=1,
                                          w_x=3,
                                          w_y=3,
                                          stride_x=4,
                                          stride_y=4,
                                          pad=True,
                                          out_activation_min=-88,
                                          out_activation_max=127)
    dataset = 'conv_out_activation'
    testdata_sets[dataset] = ConvSettings(dataset,
                                          type_of_test,
                                          regenerate_weights,
                                          regenerate_input,
                                          regenerate_biases,
                                          schema_file,
                                          in_ch=4,
                                          out_ch=2,
                                          x_in=3,
                                          y_in=3,
                                          w_x=3,
                                          w_y=3,
                                          stride_x=1,
                                          stride_y=1,
                                          pad=True,
                                          out_activation_min=-61,
                                          out_activation_max=107)
    dataset = 'conv_dilation_golden'
    testdata_sets[dataset] = ConvSettings(dataset,
                                          type_of_test,
                                          regenerate_weights,
                                          regenerate_input,
                                          regenerate_biases,
                                          schema_file,
                                          in_ch=1,
                                          batches=2,
                                          out_ch=3,
                                          x_in=6,
                                          y_in=4,
                                          w_x=2,
                                          w_y=2,
                                          stride_x=1,
                                          stride_y=1,
                                          pad=True,
                                          out_activation_min=-128,
                                          out_activation_max=127,
                                          dilation_x=3,
                                          dilation_y=2)
    dataset = 'conv_2x2_dilation'
    testdata_sets[dataset] = ConvSettings(dataset,
                                          type_of_test,
                                          regenerate_weights,
                                          regenerate_input,
                                          regenerate_biases,
                                          schema_file,
                                          in_ch=2,
                                          out_ch=2,
                                          x_in=10,
                                          y_in=10,
                                          w_x=3,
                                          w_y=3,
                                          stride_x=1,
                                          stride_y=1,
                                          pad=False,
                                          out_activation_min=-61,
                                          out_activation_max=107,
                                          dilation_x=2,
                                          dilation_y=2)
    dataset = 'conv_2x3_dilation'
    testdata_sets[dataset] = ConvSettings(dataset,
                                          type_of_test,
                                          regenerate_weights,
                                          regenerate_input,
                                          regenerate_biases,
                                          schema_file,
                                          in_ch=2,
                                          out_ch=2,
                                          x_in=3,
                                          y_in=3,
                                          w_x=3,
                                          w_y=3,
                                          stride_x=1,
                                          stride_y=1,
                                          pad=True,
                                          out_activation_min=-61,
                                          out_activation_max=107,
                                          dilation_x=2,
                                          dilation_y=2)
    dataset = 'conv_3x2_dilation'
    testdata_sets[dataset] = ConvSettings(dataset,
                                          type_of_test,
                                          regenerate_weights,
                                          regenerate_input,
                                          regenerate_biases,
                                          schema_file,
                                          in_ch=2,
                                          out_ch=2,
                                          x_in=3,
                                          y_in=3,
                                          w_x=3,
                                          w_y=3,
                                          stride_x=1,
                                          stride_y=1,
                                          pad=True,
                                          out_activation_min=-61,
                                          out_activation_max=107,
                                          dilation_x=3,
                                          dilation_y=2)
    dataset = 'conv_2x2_dilation_5x5_input'
    testdata_sets[dataset] = ConvSettings(dataset,
                                          type_of_test,
                                          regenerate_weights,
                                          regenerate_input,
                                          regenerate_biases,
                                          schema_file,
                                          in_ch=2,
                                          out_ch=2,
                                          x_in=5,
                                          y_in=5,
                                          w_x=3,
                                          w_y=3,
                                          stride_x=1,
                                          stride_y=1,
                                          pad=True,
                                          out_activation_min=-61,
                                          out_activation_max=107,
                                          dilation_x=2,
                                          dilation_y=2)
    dataset = 'conv_3x3_dilation_5x5_input'
    testdata_sets[dataset] = ConvSettings(dataset,
                                          type_of_test,
                                          regenerate_weights,
                                          regenerate_input,
                                          regenerate_biases,
                                          schema_file,
                                          in_ch=2,
                                          out_ch=2,
                                          x_in=9,
                                          y_in=11,
                                          w_x=3,
                                          w_y=3,
                                          stride_x=1,
                                          stride_y=1,
                                          pad=True,
                                          out_activation_min=-61,
                                          out_activation_max=107,
                                          dilation_x=2,
                                          dilation_y=2)
    dataset = 'int16xint8'
    testdata_sets[dataset] = ConvSettings(dataset,
                                          type_of_test,
                                          regenerate_weights,
                                          regenerate_input,
                                          regenerate_biases,
                                          schema_file,
                                          in_ch=3,
                                          out_ch=4,
                                          x_in=7,
                                          y_in=8,
                                          w_x=2,
                                          w_y=4,
                                          stride_x=2,
                                          stride_y=3,
                                          pad=True,
                                          randmin=INT16_MIN,
                                          randmax=INT16_MAX,
                                          out_activation_min=-13335,
                                          out_activation_max=32767,
                                          int16xint8=True)
    dataset = 'requantize_s64'
    testdata_sets[dataset] = ConvSettings(dataset,
                                          type_of_test,
                                          regenerate_weights,
                                          regenerate_input,
                                          regenerate_biases,
                                          schema_file,
                                          in_ch=2,
                                          out_ch=2,
                                          x_in=3,
                                          y_in=2,
                                          w_x=2,
                                          w_y=2,
                                          stride_x=1,
                                          stride_y=1,
                                          pad=False,
                                          out_activation_min=INT16_MIN,
                                          out_activation_max=INT16_MAX,
                                          int16xint8=True,
                                          bias_min=-0x300,
                                          bias_max=0x9fff)
    dataset = 'int16xint8_dilation_1'
    testdata_sets[dataset] = ConvSettings(dataset,
                                          type_of_test,
                                          regenerate_weights,
                                          regenerate_input,
                                          regenerate_biases,
                                          schema_file,
                                          in_ch=2,
                                          out_ch=2,
                                          x_in=32,
                                          y_in=32,
                                          w_x=2,
                                          w_y=2,
                                          stride_x=1,
                                          stride_y=1,
                                          pad=False,
                                          out_activation_min=INT16_MIN,
                                          out_activation_max=INT16_MAX,
                                          int16xint8=True,
                                          bias_min=-0x300,
                                          dilation_x=2,
                                          dilation_y=2)
    dataset = 'int16xint8_dilation_2'
    testdata_sets[dataset] = ConvSettings(dataset,
                                          type_of_test,
                                          regenerate_weights,
                                          regenerate_input,
                                          regenerate_biases,
                                          schema_file,
                                          in_ch=3,
                                          out_ch=4,
                                          x_in=7,
                                          y_in=8,
                                          w_x=2,
                                          w_y=4,
                                          stride_x=1,
                                          stride_y=1,
                                          pad=True,
                                          randmin=INT16_MIN,
                                          randmax=INT16_MAX,
                                          out_activation_min=-13335,
                                          out_activation_max=32767,
                                          int16xint8=True,
                                          dilation_x=2,
                                          dilation_y=2)
    dataset = 'int16xint8_dilation_3'
    testdata_sets[dataset] = ConvSettings(dataset,
                                          type_of_test,
                                          regenerate_weights,
                                          regenerate_input,
                                          regenerate_biases,
                                          schema_file,
                                          in_ch=3,
                                          out_ch=4,
                                          x_in=7,
                                          y_in=8,
                                          w_x=2,
                                          w_y=4,
                                          stride_x=1,
                                          stride_y=1,
                                          pad=True,
                                          randmin=INT16_MIN,
                                          randmax=INT16_MAX,
                                          out_activation_min=-13335,
                                          out_activation_max=32767,
                                          int16xint8=True,
                                          dilation_x=2)

    type_of_test = 'depthwise_conv'
    dataset = 'depthwise_2'
    testdata_sets[dataset] = ConvSettings(dataset,
                                          type_of_test,
                                          regenerate_weights,
                                          regenerate_input,
                                          regenerate_biases,
                                          schema_file,
                                          in_ch=3,
                                          out_ch=9,
                                          x_in=6,
                                          y_in=5,
                                          w_x=3,
                                          w_y=4,
                                          stride_x=2,
                                          stride_y=2,
                                          pad=True,
                                          out_activation_min=-73,
                                          out_activation_max=127)
    dataset = 'depthwise_kernel_3x3'
    testdata_sets[dataset] = ConvSettings(dataset,
                                          type_of_test,
                                          regenerate_weights,
                                          regenerate_input,
                                          regenerate_biases,
                                          schema_file,
                                          in_ch=5,
                                          out_ch=5,
                                          x_in=4,
                                          y_in=5,
                                          w_x=3,
                                          w_y=3,
                                          stride_x=2,
                                          stride_y=2,
                                          pad=True,
                                          bias_min=INT8_MIN,
                                          bias_max=INT8_MAX,
                                          out_activation_min=-104,
                                          out_activation_max=127)
    dataset = 'depthwise_kernel_3x3_null_bias'
    testdata_sets[dataset] = ConvSettings(dataset,
                                          type_of_test,
                                          regenerate_weights,
                                          regenerate_input,
                                          regenerate_biases,
                                          schema_file,
                                          in_ch=5,
                                          out_ch=5,
                                          x_in=4,
                                          y_in=5,
                                          w_x=3,
                                          w_y=3,
                                          stride_x=2,
                                          stride_y=2,
                                          pad=True,
                                          generate_bias=False,
                                          out_activation_min=-104,
                                          out_activation_max=127)
    dataset = 'depthwise_eq_in_out_ch'
    testdata_sets[dataset] = ConvSettings(dataset,
                                          type_of_test,
                                          regenerate_weights,
                                          regenerate_input,
                                          regenerate_biases,
                                          schema_file,
                                          in_ch=250,
                                          out_ch=250,
                                          x_in=7,
                                          y_in=5,
                                          w_x=2,
                                          w_y=2,
                                          stride_x=1,
                                          stride_y=1,
                                          pad=True)
    dataset = 'depthwise_sub_block'
    testdata_sets[dataset] = ConvSettings(dataset,
                                          type_of_test,
                                          regenerate_weights,
                                          regenerate_input,
                                          regenerate_biases,
                                          schema_file,
                                          in_ch=9,
                                          out_ch=9,
                                          x_in=7,
                                          y_in=5,
                                          w_x=2,
                                          w_y=2,
                                          stride_x=1,
                                          stride_y=1,
                                          pad=False)
    dataset = 'depthwise_x_stride'
    testdata_sets[dataset] = ConvSettings(dataset,
                                          type_of_test,
                                          regenerate_weights,
                                          regenerate_input,
                                          regenerate_biases,
                                          schema_file,
                                          in_ch=9,
                                          out_ch=9,
                                          x_in=7,
                                          y_in=5,
                                          w_x=2,
                                          w_y=2,
                                          stride_x=2,
                                          stride_y=1,
                                          pad=False)
    dataset = 'depthwise_out_activation'
    testdata_sets[dataset] = ConvSettings(dataset,
                                          type_of_test,
                                          regenerate_weights,
                                          regenerate_input,
                                          regenerate_biases,
                                          schema_file,
                                          in_ch=3,
                                          out_ch=3,
                                          x_in=6,
                                          y_in=5,
                                          w_x=3,
                                          w_y=4,
                                          pad=False,
                                          out_activation_min=-45,
                                          out_activation_max=103)
    dataset = 'depthwise_mult_batches'
    testdata_sets[dataset] = ConvSettings(dataset,
                                          type_of_test,
                                          regenerate_weights,
                                          regenerate_input,
                                          regenerate_biases,
                                          schema_file,
                                          in_ch=3,
                                          out_ch=3,
                                          x_in=3,
                                          y_in=5,
                                          w_x=2,
                                          w_y=4,
                                          stride_x=2,
                                          stride_y=2,
                                          pad=True,
                                          batches=2)
    dataset = 'depthwise_null_bias_0'
    testdata_sets[dataset] = ConvSettings(dataset,
                                          type_of_test,
                                          regenerate_weights,
                                          regenerate_input,
                                          regenerate_biases,
                                          schema_file,
                                          in_ch=2,
                                          out_ch=2,
                                          x_in=4,
                                          y_in=5,
                                          w_x=2,
                                          w_y=2,
                                          stride_x=1,
                                          stride_y=1,
                                          pad=True,
                                          generate_bias=False,
                                          batches=1)
    dataset = 'depthwise_null_bias_1'
    testdata_sets[dataset] = ConvSettings(dataset,
                                          type_of_test,
                                          regenerate_weights,
                                          regenerate_input,
                                          regenerate_biases,
                                          schema_file,
                                          in_ch=2,
                                          out_ch=16,
                                          x_in=4,
                                          y_in=5,
                                          w_x=2,
                                          w_y=2,
                                          stride_x=1,
                                          stride_y=1,
                                          pad=True,
                                          generate_bias=False,
                                          batches=1)
    dataset = 'depthwise_dilation'
    testdata_sets[dataset] = ConvSettings(dataset,
                                          type_of_test,
                                          regenerate_weights,
                                          regenerate_input,
                                          regenerate_biases,
                                          schema_file,
                                          in_ch=3,
                                          out_ch=9,
                                          x_in=6,
                                          y_in=5,
                                          w_x=3,
                                          w_y=4,
                                          stride_x=2,
                                          stride_y=2,
                                          pad=True,
                                          out_activation_min=-70,
                                          out_activation_max=127,
                                          dilation_x=2,
                                          dilation_y=3)
    dataset = 'dw_int16xint8'
    testdata_sets[dataset] = ConvSettings(dataset,
                                          type_of_test,
                                          regenerate_weights,
                                          regenerate_input,
                                          regenerate_biases,
                                          schema_file,
                                          in_ch=4,
                                          out_ch=8,
                                          x_in=9,
                                          y_in=5,
                                          w_x=3,
                                          w_y=4,
                                          stride_x=3,
                                          stride_y=2,
                                          pad=True,
                                          randmin=INT16_MIN,
                                          randmax=INT16_MAX,
                                          out_activation_min=-21111,
                                          out_activation_max=32767,
                                          int16xint8=True)
    dataset = 'dw_int16xint8_dilation'
    testdata_sets[dataset] = ConvSettings(dataset,
                                          type_of_test,
                                          regenerate_weights,
                                          regenerate_input,
                                          regenerate_biases,
                                          schema_file,
                                          in_ch=4,
                                          out_ch=8,
                                          x_in=9,
                                          y_in=5,
                                          w_x=4,
                                          w_y=4,
                                          stride_x=1,
                                          stride_y=1,
                                          pad=True,
                                          randmin=INT16_MIN,
                                          randmax=INT16_MAX,
                                          out_activation_min=-32700,
                                          dilation_x=3,
                                          dilation_y=2,
                                          out_activation_max=32767,
                                          int16xint8=True)
    dataset = 'dw_int16xint8_mult4'
    testdata_sets[dataset] = ConvSettings(dataset,
                                          type_of_test,
                                          regenerate_weights,
                                          regenerate_input,
                                          regenerate_biases,
                                          schema_file,
                                          in_ch=2,
                                          out_ch=8,
                                          x_in=4,
                                          y_in=5,
                                          w_x=3,
                                          w_y=4,
                                          stride_x=3,
                                          stride_y=2,
                                          pad=False,
                                          randmin=INT16_MIN,
                                          randmax=INT16_MAX,
                                          out_activation_min=-32767,
                                          out_activation_max=32767,
                                          int16xint8=True)
    dataset = 'dw_int16xint8_fast'
    testdata_sets[dataset] = ConvSettings(dataset,
                                          type_of_test,
                                          regenerate_weights,
                                          regenerate_input,
                                          regenerate_biases,
                                          schema_file,
                                          in_ch=8,
                                          out_ch=8,
                                          x_in=4,
                                          y_in=4,
                                          w_x=2,
                                          w_y=2,
                                          stride_x=1,
                                          stride_y=1,
                                          pad=False,
                                          randmin=INT16_MIN,
                                          randmax=INT16_MAX,
                                          out_activation_min=-17000,
                                          out_activation_max=32767,
                                          int16xint8=True)
    dataset = 'dw_int16xint8_fast_multiple_batches_uneven_buffers'
    testdata_sets[dataset] = ConvSettings(dataset,
                                          type_of_test,
                                          regenerate_weights,
                                          regenerate_input,
                                          regenerate_biases,
                                          schema_file,
                                          in_ch=8,
                                          out_ch=8,
                                          x_in=5,
                                          y_in=5,
                                          w_x=3,
                                          w_y=3,
                                          stride_x=1,
                                          stride_y=1,
                                          pad=False,
                                          randmin=INT16_MIN,
                                          randmax=INT16_MAX,
                                          out_activation_min=-17000,
                                          out_activation_max=32767,
                                          int16xint8=True,
                                          batches=3)
    dataset = 'dw_int16xint8_fast_multiple_batches_uneven_buffers_null_bias'
    testdata_sets[dataset] = ConvSettings(dataset,
                                          type_of_test,
                                          regenerate_weights,
                                          regenerate_input,
                                          regenerate_biases,
                                          schema_file,
                                          in_ch=8,
                                          out_ch=8,
                                          x_in=4,
                                          y_in=4,
                                          w_x=3,
                                          w_y=2,
                                          stride_x=1,
                                          stride_y=1,
                                          pad=False,
                                          randmin=INT16_MIN,
                                          randmax=INT16_MAX,
                                          out_activation_min=-17000,
                                          out_activation_max=32767,
                                          int16xint8=True,
                                          batches=3,
                                          generate_bias=False)

    dataset = 'dw_int16xint8_fast_test_bias'
    nbr_of_out_channels = 8
    bias = [i for i in range(nbr_of_out_channels)]
    testdata_sets[dataset] = ConvSettings(dataset,
                                          type_of_test,
                                          regenerate_weights,
                                          regenerate_input,
                                          regenerate_biases,
                                          schema_file,
                                          in_ch=8,
                                          out_ch=nbr_of_out_channels,
                                          x_in=4,
                                          y_in=4,
                                          w_x=2,
                                          w_y=2,
                                          stride_x=1,
                                          stride_y=1,
                                          pad=False,
                                          randmin=INT16_MIN,
                                          randmax=INT16_MAX,
                                          out_activation_min=-17000,
                                          out_activation_max=32767,
                                          int16xint8=True,
                                          generate_bias=bias)

    dataset = 'dw_int16xint8_fast_null_bias'
    testdata_sets[dataset] = ConvSettings(dataset,
                                          type_of_test,
                                          regenerate_weights,
                                          regenerate_input,
                                          regenerate_biases,
                                          schema_file,
                                          in_ch=8,
                                          out_ch=8,
                                          x_in=4,
                                          y_in=4,
                                          w_x=2,
                                          w_y=2,
                                          stride_x=1,
                                          stride_y=1,
                                          pad=False,
                                          randmin=INT16_MIN,
                                          randmax=INT16_MAX,
                                          out_activation_min=-17000,
                                          out_activation_max=32767,
                                          int16xint8=True,
                                          generate_bias=False)
    dataset = 'dw_int16xint8_fast_stride'
    testdata_sets[dataset] = ConvSettings(dataset,
                                          type_of_test,
                                          regenerate_weights,
                                          regenerate_input,
                                          regenerate_biases,
                                          schema_file,
                                          in_ch=8,
                                          out_ch=8,
                                          x_in=4,
                                          y_in=4,
                                          w_x=2,
                                          w_y=2,
                                          stride_x=2,
                                          stride_y=2,
                                          pad=True,
                                          randmin=INT16_MIN,
                                          randmax=INT16_MAX,
                                          batches=2,
                                          out_activation_min=INT16_MIN,
                                          out_activation_max=16000,
                                          int16xint8=True)
    dataset = 'dw_int16xint8_fast_stride_null_bias'
    testdata_sets[dataset] = ConvSettings(dataset,
                                          type_of_test,
                                          regenerate_weights,
                                          regenerate_input,
                                          regenerate_biases,
                                          schema_file,
                                          in_ch=8,
                                          out_ch=8,
                                          x_in=4,
                                          y_in=4,
                                          w_x=2,
                                          w_y=2,
                                          stride_x=2,
                                          stride_y=2,
                                          pad=True,
                                          randmin=INT16_MIN,
                                          randmax=INT16_MAX,
                                          batches=2,
                                          out_activation_min=INT16_MIN,
                                          out_activation_max=16000,
                                          int16xint8=True,
                                          generate_bias=False)
    dataset = 'dw_int16xint8_fast_spill'
    testdata_sets[dataset] = ConvSettings(dataset,
                                          type_of_test,
                                          regenerate_weights,
                                          regenerate_input,
                                          regenerate_biases,
                                          schema_file,
                                          in_ch=5,
                                          out_ch=5,
                                          x_in=4,
                                          y_in=4,
                                          w_x=3,
                                          w_y=3,
                                          stride_x=2,
                                          stride_y=1,
                                          pad=True,
                                          randmin=INT16_MIN,
                                          randmax=INT16_MAX,
                                          batches=3,
                                          out_activation_min=-30000,
                                          out_activation_max=32767,
                                          int16xint8=True)
    dataset = 'dw_int16xint8_fast_spill_null_bias'
    testdata_sets[dataset] = ConvSettings(dataset,
                                          type_of_test,
                                          regenerate_weights,
                                          regenerate_input,
                                          regenerate_biases,
                                          schema_file,
                                          in_ch=5,
                                          out_ch=5,
                                          x_in=4,
                                          y_in=4,
                                          w_x=3,
                                          w_y=3,
                                          stride_x=2,
                                          stride_y=1,
                                          pad=True,
                                          randmin=INT16_MIN,
                                          randmax=INT16_MAX,
                                          batches=3,
                                          out_activation_min=-30000,
                                          out_activation_max=32767,
                                          int16xint8=True,
                                          generate_bias=False)

    type_of_test = 'fully_connected'
    dataset = 'fully_connected'
    testdata_sets[dataset] = FullyConnectedSettings(dataset,
                                                    type_of_test,
                                                    regenerate_weights,
                                                    regenerate_input,
                                                    regenerate_biases,
                                                    schema_file,
                                                    in_ch=10,
                                                    out_ch=6,
                                                    x_in=2,
                                                    y_in=1,
                                                    batches=3)
    dataset = 'fully_connected_mve_0'
    testdata_sets[dataset] = FullyConnectedSettings(dataset,
                                                    type_of_test,
                                                    regenerate_weights,
                                                    regenerate_input,
                                                    regenerate_biases,
                                                    schema_file,
                                                    in_ch=16,
                                                    out_ch=9,
                                                    x_in=1,
                                                    y_in=1,
                                                    batches=1)
    dataset = 'fully_connected_mve_1'
    testdata_sets[dataset] = FullyConnectedSettings(dataset,
                                                    type_of_test,
                                                    regenerate_weights,
                                                    regenerate_input,
                                                    regenerate_biases,
                                                    schema_file,
                                                    in_ch=20,
                                                    out_ch=4,
                                                    x_in=1,
                                                    y_in=1,
                                                    batches=1)
    dataset = 'fully_connected_null_bias_0'
    testdata_sets[dataset] = FullyConnectedSettings(dataset,
                                                    type_of_test,
                                                    regenerate_weights,
                                                    regenerate_input,
                                                    regenerate_biases,
                                                    schema_file,
                                                    in_ch=33,
                                                    out_ch=5,
                                                    batches=2,
                                                    generate_bias=False)
    dataset = 'fully_connected_out_activation'
    testdata_sets[dataset] = FullyConnectedSettings(dataset,
                                                    type_of_test,
                                                    regenerate_weights,
                                                    regenerate_input,
                                                    regenerate_biases,
                                                    schema_file,
                                                    in_ch=10,
                                                    out_ch=4,
                                                    out_activation_min=-70,
                                                    out_activation_max=100)
    dataset = 'fully_connected_int16'
    testdata_sets[dataset] = FullyConnectedSettings(dataset,
                                                    type_of_test,
                                                    regenerate_weights,
                                                    regenerate_input,
                                                    regenerate_biases,
                                                    schema_file,
                                                    in_ch=7,
                                                    out_ch=11,
                                                    x_in=3,
                                                    y_in=3,
                                                    batches=2,
                                                    randmin=INT16_MIN,
                                                    randmax=INT16_MAX,
                                                    out_activation_min=-9999,
                                                    out_activation_max=32767,
                                                    int16xint8=True)
    dataset = 'fully_connected_int16_big'
    testdata_sets[dataset] = FullyConnectedSettings(dataset,
                                                    type_of_test,
                                                    regenerate_weights,
                                                    regenerate_input,
                                                    regenerate_biases,
                                                    schema_file,
                                                    in_ch=7,
                                                    out_ch=11,
                                                    x_in=10,
                                                    y_in=10,
                                                    batches=3,
                                                    out_activation_min=-1444,
                                                    out_activation_max=32767,
                                                    int16xint8=True)
    dataset = 'fc_int16_slow'
    testdata_sets[dataset] = FullyConnectedSettings(dataset,
                                                    type_of_test,
                                                    regenerate_weights,
                                                    regenerate_input,
                                                    regenerate_biases,
                                                    schema_file,
                                                    in_ch=7,
                                                    out_ch=11,
                                                    x_in=10,
                                                    y_in=8,
                                                    batches=3,
                                                    randmin=(INT16_MAX - 100),
                                                    randmax=INT16_MAX,
                                                    int16xint8=True)

    type_of_test = 'avgpool'
    dataset = 'avgpooling'
    testdata_sets[dataset] = PoolingSettings(dataset,
                                             type_of_test,
                                             regenerate_weights,
                                             regenerate_input,
                                             regenerate_biases,
                                             schema_file,
                                             channels=20,
                                             x_in=22,
                                             y_in=12,
                                             stride_x=9,
                                             stride_y=5,
                                             w_x=6,
                                             w_y=5,
                                             pad=True)
    dataset = 'avgpooling_1'
    testdata_sets[dataset] = PoolingSettings(dataset,
                                             type_of_test,
                                             regenerate_weights,
                                             regenerate_input,
                                             regenerate_biases,
                                             schema_file,
                                             channels=3,
                                             x_in=9,
                                             y_in=5,
                                             stride_x=1,
                                             stride_y=2,
                                             w_x=9,
                                             w_y=5,
                                             pad=False)
    dataset = 'avgpooling_2'
    testdata_sets[dataset] = PoolingSettings(dataset,
                                             type_of_test,
                                             regenerate_weights,
                                             regenerate_input,
                                             regenerate_biases,
                                             schema_file,
                                             channels=5,
                                             x_in=12,
                                             y_in=1,
                                             stride_x=1,
                                             stride_y=2,
                                             w_x=3,
                                             w_y=1,
                                             pad=True)
    dataset = 'avgpooling_3'
    testdata_sets[dataset] = PoolingSettings(dataset,
                                             type_of_test,
                                             regenerate_weights,
                                             regenerate_input,
                                             regenerate_biases,
                                             schema_file,
                                             channels=2,
                                             x_in=9,
                                             y_in=1,
                                             stride_x=2,
                                             stride_y=1,
                                             w_x=1,
                                             w_y=1,
                                             pad=False)
    dataset = 'avgpooling_4'
    testdata_sets[dataset] = PoolingSettings(dataset,
                                             type_of_test,
                                             regenerate_weights,
                                             regenerate_input,
                                             regenerate_biases,
                                             schema_file,
                                             channels=2,
                                             x_in=1,
                                             y_in=20,
                                             stride_x=1,
                                             stride_y=3,
                                             w_x=1,
                                             w_y=3,
                                             pad=True)
    dataset = 'avgpooling_5'
    testdata_sets[dataset] = PoolingSettings(dataset,
                                             type_of_test,
                                             regenerate_weights,
                                             regenerate_input,
                                             regenerate_biases,
                                             schema_file,
                                             channels=1,
                                             x_in=3,
                                             y_in=3,
                                             stride_x=1,
                                             stride_y=1,
                                             w_x=1,
                                             w_y=3,
                                             pad=True,
                                             relu6=True)
    dataset = 'avgpooling_int16'
    testdata_sets[dataset] = PoolingSettings(dataset,
                                             type_of_test,
                                             regenerate_weights,
                                             regenerate_input,
                                             regenerate_biases,
                                             schema_file,
                                             channels=17,
                                             x_in=6,
                                             y_in=4,
                                             stride_x=2,
                                             stride_y=1,
                                             w_x=2,
                                             w_y=3,
                                             pad=True,
                                             randmin=INT16_MIN,
                                             randmax=INT16_MAX,
                                             int16xint8=True)
    dataset = 'avgpooling_int16_1'
    testdata_sets[dataset] = PoolingSettings(dataset,
                                             type_of_test,
                                             regenerate_weights,
                                             regenerate_input,
                                             regenerate_biases,
                                             schema_file,
                                             channels=2,
                                             x_in=9,
                                             y_in=1,
                                             stride_x=2,
                                             stride_y=1,
                                             w_x=1,
                                             w_y=1,
                                             pad=False,
                                             randmin=INT16_MIN,
                                             randmax=INT16_MAX,
                                             int16xint8=True)
    dataset = 'avgpooling_int16_2'
    testdata_sets[dataset] = PoolingSettings(dataset,
                                             type_of_test,
                                             regenerate_weights,
                                             regenerate_input,
                                             regenerate_biases,
                                             schema_file,
                                             channels=20,
                                             x_in=9,
                                             y_in=1,
                                             stride_x=2,
                                             stride_y=1,
                                             w_x=1,
                                             w_y=1,
                                             pad=False,
                                             randmin=INT16_MIN,
                                             randmax=INT16_MAX,
                                             int16xint8=True)
    dataset = 'avgpooling_int16_3'
    testdata_sets[dataset] = PoolingSettings(dataset,
                                             type_of_test,
                                             regenerate_weights,
                                             regenerate_input,
                                             regenerate_biases,
                                             schema_file,
                                             channels=21,
                                             x_in=1,
                                             y_in=20,
                                             stride_x=1,
                                             stride_y=3,
                                             w_x=1,
                                             w_y=3,
                                             pad=True,
                                             randmin=INT16_MIN,
                                             randmax=INT16_MAX,
                                             int16xint8=True)

    type_of_test = 'maxpool'
    dataset = 'maxpooling'
    testdata_sets[dataset] = PoolingSettings(dataset,
                                             type_of_test,
                                             regenerate_weights,
                                             regenerate_input,
                                             regenerate_biases,
                                             schema_file,
                                             channels=8,
                                             x_in=22,
                                             y_in=12,
                                             stride_x=9,
                                             stride_y=5,
                                             w_x=6,
                                             w_y=5,
                                             pad=True)
    dataset = 'maxpooling_1'
    testdata_sets[dataset] = PoolingSettings(dataset,
                                             type_of_test,
                                             regenerate_weights,
                                             regenerate_input,
                                             regenerate_biases,
                                             schema_file,
                                             channels=3,
                                             x_in=9,
                                             y_in=5,
                                             stride_x=1,
                                             stride_y=2,
                                             w_x=9,
                                             w_y=5,
                                             pad=False)
    dataset = 'maxpooling_2'
    testdata_sets[dataset] = PoolingSettings(dataset,
                                             type_of_test,
                                             regenerate_weights,
                                             regenerate_input,
                                             regenerate_biases,
                                             schema_file,
                                             channels=5,
                                             x_in=12,
                                             y_in=1,
                                             stride_x=1,
                                             stride_y=2,
                                             w_x=3,
                                             w_y=1,
                                             pad=True)
    dataset = 'maxpooling_3'
    testdata_sets[dataset] = PoolingSettings(dataset,
                                             type_of_test,
                                             regenerate_weights,
                                             regenerate_input,
                                             regenerate_biases,
                                             schema_file,
                                             channels=2,
                                             x_in=9,
                                             y_in=1,
                                             stride_x=2,
                                             stride_y=1,
                                             w_x=1,
                                             w_y=1,
                                             pad=False)
    dataset = 'maxpooling_4'
    testdata_sets[dataset] = PoolingSettings(dataset,
                                             type_of_test,
                                             regenerate_weights,
                                             regenerate_input,
                                             regenerate_biases,
                                             schema_file,
                                             channels=2,
                                             x_in=1,
                                             y_in=20,
                                             stride_x=1,
                                             stride_y=3,
                                             w_x=1,
                                             w_y=3,
                                             pad=True)
    dataset = 'maxpooling_5'
    testdata_sets[dataset] = PoolingSettings(dataset,
                                             type_of_test,
                                             regenerate_weights,
                                             regenerate_input,
                                             regenerate_biases,
                                             schema_file,
                                             channels=20,
                                             x_in=1,
                                             y_in=1,
                                             stride_x=1,
                                             stride_y=1,
                                             w_x=1,
                                             w_y=1,
                                             pad=True)
    dataset = 'maxpooling_6'
    testdata_sets[dataset] = PoolingSettings(dataset,
                                             type_of_test,
                                             regenerate_weights,
                                             regenerate_input,
                                             regenerate_biases,
                                             schema_file,
                                             channels=17,
                                             x_in=1,
                                             y_in=5,
                                             stride_x=1,
                                             stride_y=3,
                                             w_x=3,
                                             w_y=4,
                                             pad=True)
    dataset = 'maxpooling_7'
    testdata_sets[dataset] = PoolingSettings(dataset,
                                             type_of_test,
                                             regenerate_weights,
                                             regenerate_input,
                                             regenerate_biases,
                                             schema_file,
                                             channels=1,
                                             x_in=4,
                                             y_in=2,
                                             stride_x=2,
                                             stride_y=2,
                                             w_x=2,
                                             w_y=2,
                                             pad=False,
                                             relu6=True)
    dataset = 'maxpool_int16'
    testdata_sets[dataset] = PoolingSettings(dataset,
                                             type_of_test,
                                             regenerate_weights,
                                             regenerate_input,
                                             regenerate_biases,
                                             schema_file,
                                             channels=2,
                                             x_in=4,
                                             y_in=3,
                                             stride_x=2,
                                             stride_y=2,
                                             w_x=2,
                                             w_y=2,
                                             pad=False,
                                             randmin=INT16_MIN,
                                             randmax=INT16_MAX,
                                             int16xint8=True)
    dataset = 'maxpool_int16_1'
    testdata_sets[dataset] = PoolingSettings(dataset,
                                             type_of_test,
                                             regenerate_weights,
                                             regenerate_input,
                                             regenerate_biases,
                                             schema_file,
                                             channels=2,
                                             x_in=4,
                                             y_in=5,
                                             stride_x=2,
                                             stride_y=1,
                                             w_x=3,
                                             w_y=3,
                                             pad=True,
                                             randmin=INT16_MIN,
                                             randmax=INT16_MAX,
                                             out_activation_min=-30000,
                                             out_activation_max=30000,
                                             int16xint8=True)
    dataset = 'maxpool_int16_2'
    testdata_sets[dataset] = PoolingSettings(dataset,
                                             type_of_test,
                                             regenerate_weights,
                                             regenerate_input,
                                             regenerate_biases,
                                             schema_file,
                                             channels=3,
                                             x_in=7,
                                             y_in=7,
                                             stride_x=1,
                                             stride_y=1,
                                             w_x=3,
                                             w_y=3,
                                             pad=False,
                                             randmin=INT16_MIN,
                                             randmax=INT16_MAX,
                                             out_activation_min=-30000,
                                             out_activation_max=30000,
                                             int16xint8=True)

    type_of_test = 'softmax'
    dataset = 'softmax'
    testdata_sets[dataset] = SoftmaxSettings(dataset,
                                             type_of_test,
                                             regenerate_weights,
                                             regenerate_input,
                                             regenerate_biases,
                                             schema_file,
                                             x_in=5,
                                             y_in=2)
    dataset = 'softmax_s16'
    testdata_sets[dataset] = SoftmaxSettings(dataset,
                                             type_of_test,
                                             regenerate_weights,
                                             regenerate_input,
                                             regenerate_biases,
                                             schema_file,
                                             x_in=10,
                                             y_in=3,
                                             int16xint8=True,
                                             randmin=INT16_MIN,
                                             randmax=INT16_MAX)
    dataset = 'softmax_s8_s16'
    testdata_sets[dataset] = SoftmaxSettings(dataset,
                                             type_of_test,
                                             regenerate_weights,
                                             regenerate_input,
                                             regenerate_biases,
                                             schema_file,
                                             x_in=12,
                                             y_in=2,
                                             inInt8outInt16=True)

    type_of_test = 'svdf'
    dataset = 'svdf'
    testdata_sets[dataset] = SVDFSettings(dataset,
                                          type_of_test,
                                          regenerate_weights,
                                          regenerate_input,
                                          regenerate_biases,
                                          schema_file,
                                          batches=2,
                                          number_inputs=2,
                                          rank=8,
                                          memory_size=8,
                                          input_size=3,
                                          number_units=3)
    dataset = 'svdf_1'
    testdata_sets[dataset] = SVDFSettings(dataset,
                                          type_of_test,
                                          regenerate_weights,
                                          regenerate_input,
                                          regenerate_biases,
                                          schema_file,
                                          batches=3,
                                          number_inputs=2,
                                          rank=1,
                                          memory_size=2,
                                          input_size=7,
                                          number_units=5)
    dataset = 'svdf_2'
    testdata_sets[dataset] = SVDFSettings(dataset,
                                          type_of_test,
                                          regenerate_weights,
                                          regenerate_input,
                                          regenerate_biases,
                                          schema_file,
                                          batches=3,
                                          number_inputs=2,
                                          rank=2,
                                          memory_size=2,
                                          input_size=7,
                                          number_units=5,
                                          generate_bias=False)
    dataset = 'svdf_3'
    testdata_sets[dataset] = SVDFSettings(dataset,
                                          type_of_test,
                                          regenerate_weights,
                                          regenerate_input,
                                          regenerate_biases,
                                          schema_file,
                                          batches=1,
                                          number_inputs=2,
                                          rank=1,
                                          memory_size=2,
                                          input_size=20,
                                          number_units=12,
                                          generate_bias=False)

    type_of_test = 'add'
    dataset = 'add'
    testdata_sets[dataset] = AddMulSettings(dataset,
                                            type_of_test,
                                            regenerate_weights,
                                            regenerate_input,
                                            regenerate_biases,
                                            schema_file,
                                            channels=8,
                                            x_in=4,
                                            y_in=4,
                                            randmin=INT8_MIN,
                                            randmax=INT8_MAX)
    dataset = 'add_s16'
    testdata_sets[dataset] = AddMulSettings(dataset,
                                            type_of_test,
                                            regenerate_weights,
                                            regenerate_input,
                                            regenerate_biases,
                                            schema_file,
                                            channels=8,
                                            x_in=4,
                                            y_in=4,
                                            randmin=INT16_MIN,
                                            randmax=INT16_MAX,
                                            out_activation_min=INT16_MIN,
                                            out_activation_max=INT16_MAX,
                                            int16xint8=True)
    dataset = 'add_s16_spill'
    testdata_sets[dataset] = AddMulSettings(dataset,
                                            type_of_test,
                                            regenerate_weights,
                                            regenerate_input,
                                            regenerate_biases,
                                            schema_file,
                                            channels=7,
                                            x_in=5,
                                            y_in=3,
                                            randmin=INT16_MIN,
                                            randmax=INT16_MAX,
                                            out_activation_min=-2000,
                                            out_activation_max=INT16_MAX,
                                            int16xint8=True)

    type_of_test = 'mul'
    dataset = 'mul'
    testdata_sets[dataset] = AddMulSettings(dataset,
                                            type_of_test,
                                            regenerate_weights,
                                            regenerate_input,
                                            regenerate_biases,
                                            schema_file,
                                            channels=8,
                                            x_in=4,
                                            y_in=5,
                                            randmin=INT8_MIN,
                                            randmax=INT8_MAX)
    dataset = 'mul_s16'
    testdata_sets[dataset] = AddMulSettings(dataset,
                                            type_of_test,
                                            regenerate_weights,
                                            regenerate_input,
                                            regenerate_biases,
                                            schema_file,
                                            channels=8,
                                            x_in=5,
                                            y_in=4,
                                            randmin=INT16_MIN,
                                            randmax=INT16_MAX,
                                            out_activation_min=INT16_MIN,
                                            out_activation_max=INT16_MAX,
                                            int16xint8=True)
    dataset = 'mul_s16_spill'
    testdata_sets[dataset] = AddMulSettings(dataset,
                                            type_of_test,
                                            regenerate_weights,
                                            regenerate_input,
                                            regenerate_biases,
                                            schema_file,
                                            channels=7,
                                            x_in=5,
                                            y_in=7,
                                            randmin=INT16_MIN,
                                            randmax=INT16_MAX,
                                            out_activation_min=INT16_MIN,
                                            out_activation_max=1000,
                                            int16xint8=True)

    type_of_test = 'lstm'
    dataset = 'lstm_1'
    testdata_sets[dataset] = LSTMSettings(dataset,
                                          type_of_test,
                                          regenerate_weights,
                                          regenerate_input,
                                          regenerate_biases,
                                          schema_file,
                                          batches=1,
                                          time_steps=10,
                                          number_inputs=22,
                                          number_units=11,
                                          time_major=True)
    dataset = 'lstm_2'
    testdata_sets[dataset] = LSTMSettings(dataset,
                                          type_of_test,
                                          regenerate_weights,
                                          regenerate_input,
                                          regenerate_biases,
                                          schema_file,
                                          batches=2,
                                          time_steps=9,
                                          number_inputs=6,
                                          number_units=7,
                                          time_major=False)
    dataset = 'lstm_one_time_step'
    testdata_sets[dataset] = LSTMSettings(dataset,
                                          type_of_test,
                                          regenerate_weights,
                                          regenerate_input,
                                          regenerate_biases,
                                          schema_file,
                                          batches=3,
                                          time_steps=1,
                                          number_inputs=22,
                                          number_units=3,
                                          time_major=False)

    return testdata_sets


if __name__ == '__main__':
    if version.parse(tf.__version__) < REQUIRED_MINIMUM_TENSORFLOW_VERSION:
        print("Unsupported tensorflow version, ", version.parse(tf.__version__))
        sys.exit(0)

    args = parse_args()

    testdataset = args.dataset
    test_type = args.testtype
    schema_file = args.schema_file

    testdata_sets = load_testdata_sets()

    if args.run_all_testsets:
        for testset_name, testset_generator in testdata_sets.items():
            if test_type and testset_generator.test_type != test_type:
                continue
            print("Generating testset {}..".format(testset_name))
            testset_generator.generate_data()
            print()

        # Check that all testsets have been loaded.
        found_test_data_sets = []
        directory = 'TestCases/TestData'
        for dir in next(os.walk(directory))[1]:
            found_test_data_sets.append(dir)
        for testset_name in found_test_data_sets:
            if testset_name not in testdata_sets:
                print("WARNING: Testset {} in {} was not loaded".format(testset_name, directory))
    elif testdataset:
        try:
            generator = testdata_sets[testdataset]
        except KeyError:
            print("WARNING: testset {} not in testset list".format(testdataset))
            if test_type == 'conv' or test_type == 'depthwise_conv':
                generator = ConvSettings(testdataset, test_type, True, True, True, schema_file)
            elif test_type == 'fully_connected':
                generator = FullyConnectedSettings(testdataset, test_type, True, True, True, schema_file)
            elif test_type == 'avgpool' or test_type == 'maxpool':
                generator = PoolingSettings(testdataset, test_type, True, True, True, schema_file)
            elif test_type == 'softmax':
                generator = SoftmaxSettings(testdataset, test_type, True, True, True, schema_file)
            elif test_type == 'svdf':
                generator = SVDFSettings(testdataset, test_type, True, True, True, schema_file)
            elif test_type == 'add' or test_type == 'mul':
                generator = AddMulSettings(testdataset, test_type, True, True, True, schema_file)
            elif test_type == 'lstm':
                generator = LSTMSettings(testdataset, test_type, True, True, True, schema_file)
            else:
                raise RuntimeError("Please specify type of test with -t")
        generator.generate_data()
    else:
        raise RuntimeError("Please select testdataset or use --run-all-testsets")
