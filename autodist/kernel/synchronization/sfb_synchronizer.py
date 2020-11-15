# Copyright 2020 Petuum, Inc. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""AllReduce Synchronizer."""
from collections import defaultdict

import tensorflow
from tensorflow.python import ops
from tensorflow.python.framework import device_spec
from tensorflow.python.ops import collective_ops, math_ops
from tensorflow.python.framework.ops import Tensor

import autodist
# from autodist.const import ENV
from autodist.kernel.common.utils import get_consumers, update_consumers, \
    replica_prefix  # , get_control_consumers, update_control_consumers
from autodist.kernel.common.utils import get_op_name
from autodist.kernel.synchronization.collective_key import get_collective_keys
# from autodist.kernel.synchronization.compressor import Compressor, CollectiveOpsConfig
from autodist.kernel.synchronization.compressor import Compressor
from autodist.kernel.synchronization.synchronizer import Synchronizer
from autodist.proto import synchronizers_pb2, compressor_pb2, strategy_pb2
from autodist.utils import logging


class CollectiveOpsConfig:
    """Config for using Collective Ops."""

    group_size: int
    group_key: str
    instance_key: str
    shape: tensorflow.TensorShape
    dtype: tensorflow.dtypes.DType
    # merge_op: str
    # final_op: str


class SFBSynchronizer(Synchronizer):
    """
    Sufficient Factor Broadcasting Synchronizer.

    This AllReduce Synchronizer currently uses TensorFlow's `collective_device_ops`
    to insert their AllReduce ops into our graph.

    The class AllReduceSynchronizer class contains the following possible instantiations:

    1. spec=`auto`: single-node multiple devices, or cross-node AllReduce based on collective ops
    2. spec=`nccl`: single-node multiple devices, or cross-node AllReduce based on NCCL
    3. spec=`ring`/'tree', AllReduce with different reduction structures: ring, tree, etc.

    However note that it does not contain the following instantiations:

    1. shuffle reduce (reduce to CPU or GPU as in PS) + AllReduce across nodes
    2. any other types of hybrid reduction of PS and AllReduce.
    """

    def __init__(self, config: strategy_pb2.Strategy.Node):
        # compressor_value = getattr(config, 'compressor')
        compressor_value = getattr(config.compressor, 'type')
        syncer_config = getattr(config, config.WhichOneof('synchronizer'))
        self._spec = synchronizers_pb2.SFBSynchronizer.Spec.Name(syncer_config.spec)
        if autodist.float_major_minor_tf_version < 1.15 or autodist.float_major_minor_tf_version < 2.1:
            logging.warning('Collective synchronizer spec "{}" a.k.a communication_hint has no effect '
                            'until tensorflow-gpu 1.x>= 1.15 or 2.x>=2.1. It may cause error currently.'
                            .format(self._spec))
            self._spec = None

        # Collective ops within the same group will be merged by the scoped optimizer.
        # Normally the group index shall be smaller than the number of variables in the graph; this kernel assumes
        # the strategy will validate the group assignments are legitimate.
        self._group = syncer_config.group
        super().__init__()
        if compressor_value is not None:
            self._compressor_type = compressor_pb2.Compressor.Type.Name(compressor_value)

    @staticmethod
    def _broadcast_send(tensor: Tensor, conf: CollectiveOpsConfig):
        """
        Using CollectiveOps, broadcasting send the given tensor.

        Args:
            tensor (Tensor): the tensor to all-reduce
            conf (CollectiveOpsConfig): the config for CollectiveOps

        Returns:
            The sent Tensor
        """
        return collective_ops.broadcast_send(tensor, **conf.__dict__)

    @staticmethod
    def _broadcast_recv(conf: CollectiveOpsConfig):
        """
        Using CollectiveOps, broadcasting receive the given tensor.

        Args:
            tensor (Tensor): the tensor to all-reduce
            conf (CollectiveOpsConfig): the config for CollectiveOps

        Returns:
            The received Tensor
        """
        return collective_ops.broadcast_recv(**conf.__dict__)

    def in_graph_apply(self, graph_item, var_name):
        """
        Perform in-graph synchronization based on AllReduce and TensorFlow Collective Ops.

        Note that collective ops now only supports dense tensors.

        Args:
            graph_item (graph_item.GraphItem): the graph_item to be distributed
            var_name (str): the corresponded variable name

        Returns:
            graph_item.GraphItem: The new graph
        """
        # Skip sfb synchronizer when rank <= 1
        if self.num_replicas * self.num_workers <= 1:
            return graph_item

        item = graph_item
        var_op_name = get_op_name(var_name)

        # Throw an error if the variable is sparse
        # master_op_name = ops.prepend_name_scope(var_op_name, replica_prefix(0))
        # grad, _, _ = graph_item.var_op_name_to_grad_info[master_op_name]
        with item.graph.as_default():
            self._share_initializer(item, var_op_name, master_replica=0)
            self._collect_dense_gradients(item, var_op_name)
        return item

    # pylint: disable-msg=too-many-locals
    def _collect_dense_gradients(self, graph_item, var_op_name):
        """Append collective ops after the gradient is calculated."""
        if self.num_replicas * self.num_workers <= 1:
            raise ValueError('CollectiveOps requires collective group size > 1')

        compressors = defaultdict(lambda: Compressor.create(self._compressor_type, var_op_name))

        conf_u = CollectiveOpsConfig()
        conf_v = CollectiveOpsConfig()
        conf_u.group_size = len(self.all_canonical_replica_devices)
        conf_v.group_size = len(self.all_canonical_replica_devices)
        conf_u.group_key = get_collective_keys().get_group_key(self.all_canonical_replica_devices)
        conf_v.group_key = get_collective_keys().get_group_key(self.all_canonical_replica_devices)

        if self._spec:
            setattr(conf_u, 'communication_hint', self._spec)
            setattr(conf_v, 'communication_hint', self._spec)

        for i in range(0, self.num_replicas):
            tensors_u = []
            tensors_v = []
            for j in range(0, self.num_replicas):
                op_name = ops.prepend_name_scope(var_op_name, replica_prefix(j))
                # conf.instance_key = get_collective_keys().get_instance_key(op_name)
                grad, _, _ = graph_item.var_op_name_to_grad_info[op_name]
                v, u = grad.op.inputs
                conf_u.shape = u.shape
                conf_v.shape = v.shape
                conf_u.dtype = u.dtype
                conf_v.dtype = v.dtype
                conf_u.instance_key = get_collective_keys().get_instance_key(op_name + 'u')
                conf_v.instance_key = get_collective_keys().get_instance_key(op_name + 'v')
                if i == j:
                    tensors_u.append(self._broadcast_send(u, conf_u))
                    tensors_v.append(self._broadcast_send(v, conf_v))
                else:
                    tensors_u.append(self._broadcast_recv(conf_u))
                    tensors_v.append(self._broadcast_recv(conf_v))
            op_name = ops.prepend_name_scope(var_op_name, replica_prefix(j))
            grad, _, _ = graph_item.var_op_name_to_grad_info[op_name]
            grad_consumers = get_consumers(grad.op)
            received_grads = [compressors[i].decompress(v, u) for u, v in zip(tensors_u, tensors_v)]
            with ops.name_scope(replica_prefix(i) + "/collective-group-{}/".format(self._group)):
                # compressed_grad = compressors[i].compress(grad)
                with ops.colocate_with(grad.op):
                    combined_grad = math_ops.add_n(received_grads)
            update_consumers(grad_consumers, grad, combined_grad)

    # def _collect_sparse_gradients(self, graph_item, var_op_name):
    #     """Append collective ops after the gradient is calculated."""
    #     if self.num_workers > 1 and not ENV.AUTODIST_INTERNAL_TF.value:
    #         raise NotImplementedError('Currently the collective NCCL AllGather is not supported in TensorFlow.'
    #                                   'Please choose another strategy.')
    #     conf = {}
    #     if self._spec:
    #         conf = {'communication_hint': self._spec}
    #     if self._compressor_type:
    #         logging.warning('AllGather currently does not support AutoDist compressor so it skips.')
    #     if self.num_replicas * self.num_workers <= 1:
    #         raise ValueError('CollectiveOps requires collective group size > 1')
    #     for i in range(0, self.num_replicas):
    #         op_name = ops.prepend_name_scope(var_op_name, replica_prefix(i))
    #         grad, _, _ = graph_item.var_op_name_to_grad_info[op_name]
    #         # TODO (Tairui): (3) Merge of reduction for performance
    #         indices_c_ops = grad.indices.consumers()
    #         indices_cc_ops = get_control_consumers(grad.indices.op)
    #         values_c_ops = grad.values.consumers()
    #         values_cc_ops = get_control_consumers(grad.values.op)
    #         with ops.name_scope(replica_prefix(i)):
    #             with ops.colocate_with(grad.indices.op):
    #                 new_indices = collective_ops.all_gather(
    #                     grad.indices,
    #                     self.num_replicas * self.num_workers,
    #                     get_collective_keys().get_group_key(self.all_canonical_replica_devices),
    #                     get_collective_keys().get_instance_key(var_op_name + '-indices'),
    #                     **conf
    #                 )
    #             with ops.colocate_with(grad.values.op):
    #                 new_values = collective_ops.all_gather(
    #                     grad.values,
    #                     self.num_replicas * self.num_workers,
    #                     get_collective_keys().get_group_key(self.all_canonical_replica_devices),
    #                     get_collective_keys().get_instance_key(var_op_name + '-values'),
    #                     **conf
    #                 )
    #         update_consumers(indices_c_ops, grad.indices, new_indices)
    #         update_control_consumers(indices_cc_ops, grad.indices.op, new_indices.op)
    #         update_consumers(values_c_ops, grad.values, new_values)
    #         update_control_consumers(values_cc_ops, grad.values.op, new_values)

    def _share_initializer(self, graph_item, var_op_name, master_replica=0):
        """Share the initializers of all replica variables to use initializer on replica=master_replica."""
        # find the initial value of the var on master_replica
        master_var_op = graph_item.graph.get_operation_by_name(
            ops.prepend_name_scope(var_op_name, replica_prefix(master_replica)))
        master_var = graph_item.trainable_var_op_to_var[master_var_op]
        master_init_tensor = graph_item.graph.get_tensor_by_name(master_var.initial_value.name)
        master_init_op = master_init_tensor.op
        # set the device of the init ops to reside on the chief device
        master_init_device = device_spec.DeviceSpecV2.from_string(master_init_op.device) \
            .replace(task=0)
        master_init_op._set_device_from_string(master_init_device.to_string())

        for i in range(0, self.num_replicas):
            if i == master_replica:
                continue
            var_op = graph_item.graph.get_operation_by_name(
                ops.prepend_name_scope(var_op_name, replica_prefix(i)))
            var = graph_item.trainable_var_op_to_var[var_op]
            init_op = graph_item.graph.get_tensor_by_name(var.initial_value.name).op
            init_assign_op = get_consumers(init_op)[0]
            init_assign_op._update_input(1, master_init_tensor)

    # pylint: disable=no-self-use
    def between_graph_apply(self, graph_item, var_name):
        """Allreduce synchronizer will do nothing in between-graph synchronization."""
        return graph_item