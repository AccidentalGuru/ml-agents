from mlagents.torch_utils import torch
from typing import Tuple, Optional, List
from mlagents.trainers.torch.layers import LinearEncoder, Initialization, linear_layer


class MultiHeadAttention(torch.nn.Module):
    """
    Multi Head Attention module. We do not use the regular Torch implementation since
    Barracuda does not support some operators it uses.
    Takes as input to the forward method 3 tensors:
     - query: of dimensions (batch_size, number_of_queries, embedding_size)
     - key: of dimensions (batch_size, number_of_keys, embedding_size)
     - value: of dimensions (batch_size, number_of_keys, embedding_size)
    The forward method will return 2 tensors:
     - The output: (batch_size, number_of_queries, embedding_size)
     - The attention matrix: (batch_size, num_heads, number_of_queries, number_of_keys)
    """

    NEG_INF = -1e6

    def __init__(self, embedding_size: int, num_heads: int):
        super().__init__()
        self.n_heads = num_heads
        self.head_size: int = embedding_size // self.n_heads
        self.embedding_size: int = self.head_size * self.n_heads

    def forward(
        self,
        query: torch.Tensor,
        key: torch.Tensor,
        value: torch.Tensor,
        n_q: int,
        n_k: int,
        key_mask: Optional[torch.Tensor] = None,
    ) -> Tuple[torch.Tensor, torch.Tensor]:
        b = -1  # the batch size

        query = query.reshape(
            b, n_q, self.n_heads, self.head_size
        )  # (b, n_q, h, emb / h)
        key = key.reshape(b, n_k, self.n_heads, self.head_size)  # (b, n_k, h, emb / h)
        value = value.reshape(
            b, n_k, self.n_heads, self.head_size
        )  # (b, n_k, h, emb / h)

        query = query.permute([0, 2, 1, 3])  # (b, h, n_q, emb / h)
        # The next few lines are equivalent to : key.permute([0, 2, 3, 1])
        # This is a hack, ONNX will compress two permute operations and
        # Barracuda will not like seeing `permute([0,2,3,1])`
        key = key.permute([0, 2, 1, 3])  # (b, h, emb / h, n_k)
        key -= 1
        key += 1
        key = key.permute([0, 1, 3, 2])  # (b, h, emb / h, n_k)

        qk = torch.matmul(query, key)  # (b, h, n_q, n_k)

        if key_mask is None:
            qk = qk / (self.embedding_size ** 0.5)
        else:
            key_mask = key_mask.reshape(b, 1, 1, n_k)
            qk = (1 - key_mask) * qk / (
                self.embedding_size ** 0.5
            ) + key_mask * self.NEG_INF

        att = torch.softmax(qk, dim=3)  # (b, h, n_q, n_k)

        value = value.permute([0, 2, 1, 3])  # (b, h, n_k, emb / h)
        value_attention = torch.matmul(att, value)  # (b, h, n_q, emb / h)

        value_attention = value_attention.permute([0, 2, 1, 3])  # (b, n_q, h, emb / h)
        value_attention = value_attention.reshape(
            b, n_q, self.embedding_size
        )  # (b, n_q, emb)

        return value_attention, att


class EntityEmbeddings(torch.nn.Module):
    """
    """

    def __init__(
        self,
        x_self_size: int,
        entity_sizes: List[int],
        entity_num_max_elements: List[int],
        embedding_size: int,
        concat_self: bool = True,
    ):
        super().__init__()
        self.self_size: int = x_self_size
        self.entity_sizes: List[int] = entity_sizes
        self.entity_num_max_elements: List[int] = entity_num_max_elements
        self.concat_self: bool = concat_self
        # If not concatenating self, input to encoder is just entity size
        if not concat_self:
            self.self_size = 0
        self.ent_encoders = torch.nn.ModuleList(
            [
                LinearEncoder(self.self_size + ent_size, 1, embedding_size)
                for ent_size in self.entity_sizes
            ]
        )

    def forward(
        self, x_self: torch.Tensor, entities: List[torch.Tensor]
    ) -> Tuple[torch.Tensor, int]:
        if self.concat_self:
            # Concatenate all observations with self
            self_and_ent: List[torch.Tensor] = []
            for num_entities, ent in zip(self.entity_num_max_elements, entities):
                expanded_self = x_self.reshape(-1, 1, self.self_size)
                expanded_self = torch.cat([expanded_self] * num_entities, dim=1)
                self_and_ent.append(torch.cat([expanded_self, ent], dim=2))
        else:
            self_and_ent = entities
            # Encode and concatenate entites
        encoded_entities = torch.cat(
            [ent_encoder(x) for ent_encoder, x in zip(self.ent_encoders, self_and_ent)],
            dim=1,
        )
        return encoded_entities

    @staticmethod
    def get_masks(observations: List[torch.Tensor]) -> List[torch.Tensor]:
        """
        Takes a List of Tensors and returns a List of mask Tensor with 1 if the input was
        all zeros (on dimension 2) and 0 otherwise. This is used in the Attention
        layer to mask the padding observations.
        """
        with torch.no_grad():
            # Generate the masking tensors for each entities tensor (mask only if all zeros)
            key_masks: List[torch.Tensor] = [
                (torch.sum(ent ** 2, axis=2) < 0.01).type(torch.FloatTensor)
                for ent in observations
            ]
        return key_masks


class ResidualSelfAttention(torch.nn.Module):
    """
    A simple architecture inspired from https://arxiv.org/pdf/1909.07528.pdf that uses
    multi head self attention to encode information about a "Self" and a list of
    relevant "Entities".
    """

    EPSILON = 1e-7

    def __init__(
        self,
        embedding_size: int,
        entity_num_max_elements: List[int],
        num_heads: int = 4,
    ):
        super().__init__()
        self.entity_num_max_elements: List[int] = entity_num_max_elements
        self.max_num_ent = sum(entity_num_max_elements)
        self.attention = MultiHeadAttention(
            num_heads=num_heads, embedding_size=embedding_size
        )

        self.fc_q = linear_layer(
            embedding_size,
            embedding_size,
            kernel_init=Initialization.Normal,
            kernel_gain=(0.125 / embedding_size) ** 0.5,
        )
        self.fc_k = linear_layer(
            embedding_size,
            embedding_size,
            kernel_init=Initialization.Normal,
            kernel_gain=(0.125 / embedding_size) ** 0.5,
        )
        self.fc_v = linear_layer(
            embedding_size,
            embedding_size,
            kernel_init=Initialization.Normal,
            kernel_gain=(0.125 / embedding_size) ** 0.5,
        )
        self.fc_out = linear_layer(
            embedding_size,
            embedding_size,
            kernel_init=Initialization.Normal,
            kernel_gain=(0.125 / embedding_size) ** 0.5,
        )

    def forward(self, inp: torch.Tensor, key_masks: List[torch.Tensor]) -> torch.Tensor:
        # Gather the maximum number of entities information
        mask = torch.cat(key_masks, dim=1)
        # Feed to self attention
        query = self.fc_q(inp)  # (b, n_q, emb)
        key = self.fc_k(inp)  # (b, n_k, emb)
        value = self.fc_v(inp)  # (b, n_k, emb)
        output, _ = self.attention(
            query, key, value, self.max_num_ent, self.max_num_ent, mask
        )
        # Residual
        output = self.fc_out(output) + inp
        # Average Pooling
        numerator = torch.sum(
            output * (1 - mask).reshape(-1, self.max_num_ent, 1), dim=1
        )
        denominator = torch.sum(1 - mask, dim=1, keepdim=True) + self.EPSILON
        output = numerator / denominator
        # Residual between x_self and the output of the module
        return output
