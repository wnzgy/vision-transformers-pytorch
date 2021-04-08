import math
from typing import Sequence, Tuple

import torch
from torch import nn
from torch.nn import functional as F
from tensorfn.config import config_model
from pydantic import StrictInt, StrictFloat

from .layer import DropPath, tuple2

LayerNorm = lambda x: nn.LayerNorm(x, eps=1e-6)


class PositionwiseFeedForward(nn.Sequential):
    def __init__(self, in_dim, dim=None, out_dim=None, activation=nn.SiLU, dropout=0):
        dim = in_dim if dim is None else dim
        out_dim = in_dim if out_dim is None else out_dim

        super().__init__(
            nn.Linear(in_dim, dim),
            activation(),
            nn.Linear(dim, out_dim),
            nn.Dropout(dropout),
        )


def patchify(input, size):
    batch, height, width, dim = input.shape

    return (
        input.view(batch, height // size, size, width // size, size, dim)
        .permute(0, 1, 3, 2, 4, 5)
        .reshape(batch, height // size, width // size, -1)
    )


# Revised masking using Bernhard Walser's code
# from https://github.com/berniwal/swin-transformer-pytorch
# Much more cleaner than my mess. :)


def create_mask(window_size, displacement, upper_lower, left_right):
    mask = torch.zeros(window_size ** 2, window_size ** 2)

    if upper_lower:
        mask[-displacement * window_size :, : -displacement * window_size] = 1
        mask[: -displacement * window_size, -displacement * window_size :] = 1

    if left_right:
        mask = mask.reshape(window_size, window_size, window_size, window_size)
        mask[:, -displacement:, :, :-displacement] = float("-inf")
        mask[:, :-displacement, :, -displacement:] = float("-inf")
        mask = mask.reshape(window_size ** 2, window_size ** 2)

    return mask


def get_relative_distances(window_size):
    indices = torch.cartesian_prod(torch.arange(7), torch.arange(7))
    distances = indices[None, :, :] - indices[:, None, :]

    return distances


class MultiHeadedLocalAttention(nn.Module):
    def __init__(
        self, dim, n_head, dim_head, input_size, window_size, shift, dropout=0
    ):
        super().__init__()

        self.dim_head = dim_head
        self.n_head = n_head

        self.weight = nn.Linear(dim, n_head * dim_head * 3, bias=False)
        self.linear = nn.Linear(n_head * dim_head, dim)

        self.input_size = input_size
        self.window_size = window_size
        self.dropout = dropout
        self.shift = shift

        if shift:
            roll = window_size // 2
            self.register_buffer(
                "ul_mask", create_mask(window_size, roll, True, False) > 0
            )
            self.register_buffer(
                "lr_mask", create_mask(window_size, roll, False, True) > 0
            )

        pos = get_relative_distances(window_size) + window_size - 1
        pos_y, pos_x = pos.unbind(-1)
        self.register_buffer("pos", pos_y * (2 * window_size - 1) + pos_x)
        self.rel_pos = nn.Embedding((2 * window_size - 1) ** 2, n_head)
        self.rel_pos.weight.detach().zero_()

    def forward(self, input):
        batch, height, width, dim = input.shape
        h_stride = height // self.window_size
        w_stride = width // self.window_size
        window = self.window_size

        if self.shift:
            roll = -math.floor(window / 2)
            input = torch.roll(input, (roll, roll), (1, 2))

        def reshape(input):
            return (
                input.reshape(
                    batch,
                    h_stride,
                    window,
                    w_stride,
                    window,
                    self.n_head,
                    self.dim_head,
                )
                .permute(0, 5, 1, 3, 2, 4, 6)
                .reshape(batch, self.n_head, -1, window * window, self.dim_head)
            )

        query, key, value = self.weight(input).chunk(3, dim=-1)  # B, S, H, W^2, D

        query = reshape(query)
        key = reshape(key).transpose(-2, -1)
        value = reshape(value)

        score = query @ key / math.sqrt(self.dim_head)  # B, H, S, W^2, W^2
        rel_pos = self.rel_pos(self.pos)  # W^2, W^2, H
        score = score + rel_pos.permute(2, 0, 1).reshape(
            1, self.n_head, 1, window * window, window * window
        )

        if self.shift:
            score[:, :, -w_stride:].masked_fill_(self.ul_mask, float("-inf"))
            score[:, :, w_stride - 1 :: w_stride].masked_fill_(
                self.lr_mask, float("-inf")
            )

        attn = F.softmax(score, -1)
        attn = F.dropout(attn, self.dropout, training=self.training)

        out = attn @ value  # B, S, H, W^2, D

        out = (
            out.view(
                batch, self.n_head, h_stride, w_stride, window, window, self.dim_head
            )
            .permute(0, 2, 4, 3, 5, 1, 6)
            .reshape(batch, height, width, self.n_head * self.dim_head)
        )
        out = self.linear(out)

        if self.shift:
            out = torch.roll(out, (-roll, -roll), (1, 2))

        return out


class TransformerLayer(nn.Module):
    def __init__(
        self,
        dim,
        n_head,
        dim_head,
        dim_ff,
        input_size,
        window_size,
        shift,
        activation=nn.SiLU,
        drop_ff=0,
        drop_attn=0,
        drop_path=0,
    ):
        super().__init__()

        self.norm_attn = LayerNorm(dim)
        self.attn = MultiHeadedLocalAttention(
            dim, n_head, dim_head, input_size, window_size, shift, drop_attn
        )
        self.drop_path = DropPath(drop_path)
        self.norm_ff = LayerNorm(dim)
        self.ff = PositionwiseFeedForward(
            dim, dim_ff, activation=activation, dropout=drop_ff
        )

    def set_drop_path(self, p):
        self.drop_path.p = p

    def forward(self, input):
        out = input + self.drop_path(self.attn(self.norm_attn(input)))
        out = out + self.drop_path(self.ff(self.norm_ff(out)))

        return out


class PatchEmbedding(nn.Module):
    def __init__(self, in_dim, out_dim, window_size):
        super().__init__()

        self.window_size = window_size
        self.linear = nn.Linear(in_dim * window_size * window_size, out_dim)
        self.norm = nn.LayerNorm(out_dim)

    def forward(self, input):
        out = patchify(input, self.window_size)
        out = self.linear(out)
        out = self.norm(out)

        return out


def reduce_size(size, reduction):
    return (size[0] // reduction, size[1] // reduction)


@config_model(name="swin_transformer", use_type=True)
class SwinTransformer(nn.Module):
    def __init__(
        self,
        image_size: Tuple[StrictInt, StrictInt],
        n_class: StrictInt,
        depths: Tuple[StrictInt, StrictInt, StrictInt, StrictInt],
        dims: Tuple[StrictInt, StrictInt, StrictInt, StrictInt],
        dim_head: StrictInt,
        n_heads: Tuple[StrictInt, StrictInt, StrictInt, StrictInt],
        dim_ffs: Tuple[StrictInt, StrictInt, StrictInt, StrictInt],
        window_size: StrictInt,
        drop_ff: StrictFloat = 0.0,
        drop_attn: StrictFloat = 0.0,
        drop_path: StrictFloat = 0.0,
    ):
        super().__init__()

        self.depths = depths

        def make_block(i, in_dim, input_size, reduction):
            return self.make_block(
                depths[i],
                in_dim,
                dims[i],
                n_heads[i],
                dim_head,
                dim_ffs[i],
                input_size,
                window_size,
                reduction,
                drop_ff,
                drop_attn,
                drop_path,
            )

        self.block1 = make_block(0, 3, image_size, 4)
        self.block2 = make_block(1, dims[0], reduce_size(image_size, 4), 2)
        self.block3 = make_block(2, dims[1], reduce_size(image_size, 4 * 2), 2)
        self.block4 = make_block(3, dims[2], reduce_size(image_size, 4 * 2 * 2), 2)

        self.final_linear = nn.Sequential(nn.LayerNorm(dims[-1]))
        linear = nn.Linear(dims[-1], n_class)
        nn.init.normal_(linear.weight, std=0.01)
        nn.init.zeros_(linear.bias)
        self.classifier = nn.Sequential(nn.AdaptiveAvgPool2d(1), nn.Flatten(1), linear)

        self.apply(self.init_weights)

    def init_weights(self, module):
        if isinstance(module, nn.Linear):
            nn.init.normal_(module.weight, std=0.02)

            if module.bias is not None:
                nn.init.zeros_(module.bias)

        elif isinstance(module, nn.LayerNorm):
            nn.init.ones_(module.weight)
            nn.init.zeros_(module.bias)

    def make_block(
        self,
        depth,
        in_dim,
        dim,
        n_head,
        dim_head,
        dim_ff,
        input_size,
        window_size,
        reduction,
        drop_ff,
        drop_attn,
        drop_path,
    ):
        block = [PatchEmbedding(in_dim, dim, reduction)]

        for i in range(depth):
            block.append(
                TransformerLayer(
                    dim,
                    n_head,
                    dim_head,
                    dim_ff,
                    reduce_size(input_size, reduction),
                    window_size,
                    shift=i % 2 == 0,
                    drop_ff=drop_ff,
                    drop_attn=drop_attn,
                    drop_path=drop_path,
                )
            )

        return nn.Sequential(*block)

    def forward(self, input):
        out = self.block1(input.permute(0, 2, 3, 1))
        out = self.block2(out)
        out = self.block3(out)
        out = self.block4(out)
        out = self.final_linear(out).permute(0, 3, 1, 2)
        out = self.classifier(out)

        return out
