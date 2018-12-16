from __future__ import unicode_literals, print_function, division
from io import open
import unicodedata
import string
import re
import random

import torch
import torch.nn as nn
from torch import optim
import torch.nn.functional as F

from hier2hier.util import blockProfiler, methodProfiler, lastCallProfile

from .moduleBase import ModuleBase 
from .nodeInfoEncoder import NodeInfoEncoder
from .nodeInfoPropagator import NodeInfoPropagator
from .outputDecoder import OutputDecoder

class Hier2hier(ModuleBase):
    def __init__(self,
            modelArgs,
            tagsVocab,
            textVocab,
            attribsVocab,
            attribValueVocab,
            outputVocab,
            sos_id,
            eos_id,
            device=None):
        super().__init__(device)

        self.max_output_len = modelArgs.max_output_len
        self.outputVocab = outputVocab
        self.nodeInfoEncoder = NodeInfoEncoder(
            tagsVocab,
            textVocab,
            attribsVocab,
            attribValueVocab,
            modelArgs.max_node_count,
            modelArgs.max_node_text_len,
            modelArgs.node_text_vec_len,
            modelArgs.max_attrib_value_len,
            modelArgs.attrib_value_vec_len,
            device=device)

        self.nodeInfoPropagator = NodeInfoPropagator(
            self.nodeInfoEncoder.output_vec_len,
            modelArgs.propagated_info_len,
            modelArgs.max_node_count,
            modelArgs.node_info_propagator_stack_depth,
            device=device)

        self.outputDecoder = OutputDecoder(
            outputVocab,
            modelArgs.propagated_info_len,
            modelArgs.output_decoder_state_width,
            modelArgs.output_decoder_stack_depth,
            modelArgs.max_node_count,
            modelArgs.max_output_len,
            sos_id,
            eos_id,
            modelArgs.teacher_forcing_ratio,
            modelArgs.input_dropout_p,
            modelArgs.dropout_p,
            modelArgs.use_attention,
            device=device,
            )
        if device is not None:
            super().cuda(device)
        else:
            super().cpu()

    @methodProfiler
    def forward(self,
            xmlTreeList,
            targetOutput=None,
            target_lengths=None,
            tensorboard_hook=None):
        node2Index = {}
        node2Parent = {}
        treeIndex2NodeIndex2NbrIndices = {}
        for treeIndex, xmlTree in enumerate(xmlTreeList):
            # Build map from node to a unique index for all nodes(including root) in the current tree.
            for nodeIndex, node in enumerate(xmlTree.iter()):
                node2Index[node] = nodeIndex

            # Initialize nodeIndex2NbrIndices and create an entry for root in it.
            nodeIndex2NbrIndices = {}

            # Link nodeIndex2NbrIndices with the global treeIndex2NodeIndex2NbrIndices.
            treeIndex2NodeIndex2NbrIndices[treeIndex] = nodeIndex2NbrIndices

            # Parent of root is root.
            # Create an entry for tree root in nodeIndex2NbrIndices.
            rootIndex = node2Index[xmlTree.getroot()]
            nodeIndex2NbrIndices[rootIndex] = (rootIndex, [])

            # Create an entry for tree root in node2Parent.
            node2Parent[xmlTree.getroot()] = xmlTree.getroot()

            # Build map from node to its parent for all nodes in current tree.
            for node in xmlTree.iter():
                # The following entry works only because xmlTree iter() is traversing in top down ancestor first manner.
                curNodeChildrenList = nodeIndex2NbrIndices[node2Index[node]][1]
                for childNode in node:
                    node2Parent[childNode] = node
                    nodeIndex2NbrIndices[node2Index[childNode]] = (node2Index[node], [])
                    curNodeChildrenList.append(node2Index[childNode])


        nodeInfoTensor = self.nodeInfoEncoder(node2Index, node2Parent, xmlTreeList)
        nodeInfoPropagatedTensor = self.nodeInfoPropagator(treeIndex2NodeIndex2NbrIndices, nodeInfoTensor)
        outputSymbolTensors, outputSymbols = self.outputDecoder(treeIndex2NodeIndex2NbrIndices, nodeInfoPropagatedTensor, targetOutput, target_lengths)

        if tensorboard_hook is not None:
            tensorboard_hook.add_histogram('nodeInfoTensor', nodeInfoTensor) 
            tensorboard_hook.add_histogram('nodeInfoPropagatedTensor', nodeInfoPropagatedTensor) 
            tensorboard_hook.add_histogram('outputSymbolTensors', outputSymbolTensors) 

        return outputSymbolTensors, outputSymbols

    @methodProfiler
    def decodeOutput(self, textOutputs, textLengths=None):
        decodedOutputs = []
        sos_id = self.outputDecoder.sos_id
        eos_id = self.outputDecoder.eos_id

        for index, textOutput in enumerate(textOutputs):
            textLength = textLengths[index] if textLengths is not None else self.max_output_len
            if textOutput[0] != sos_id:
                raise ValueError("sos_id missing at index 0.")
            if textLengths is not None:
                if textOutput[int(textLengths[index])-1] != eos_id:
                    raise ValueError("eos_id missing at end index {0}.".format(textLengths[index]))

                indexRange = range(1, textLength[index])
            else:
                indexRange = range(1, self.max_output_len)

            decodedOutput = ""
            foundEos = False

            for textIndex in indexRange:
                if textOutput[textIndex] == eos_id:
                    foundEos = True
                    break
                decodedOutput += self.outputVocab.itos[textOutput[textIndex]]

            if not foundEos:
                decodedOutput += "..."
            decodedOutputs.append(decodedOutput)

        return decodedOutputs


