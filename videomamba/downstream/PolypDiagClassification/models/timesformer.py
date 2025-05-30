# Copyright (c) Facebook, Inc. and its affiliates. All Rights Reserved.
# Copyright 2020 Ross Wightman
# Modified Model definition

from functools import partial

import torch
import torch.nn as nn
import torch.nn.functional as F
from einops import rearrange

from models.helpers import load_pretrained
from models.vit_utils import DropPath, to_2tuple, trunc_normal_
from models.vit_utils import IMAGENET_DEFAULT_MEAN, IMAGENET_DEFAULT_STD
from models.endomamba_classification import endomamba_small


def _cfg(url='', **kwargs):
    return {
        'url': url,
        'num_classes': 1000, 'input_size': (3, 224, 224), 'pool_size': None,
        'crop_pct': .9, 'interpolation': 'bicubic',
        'mean': IMAGENET_DEFAULT_MEAN, 'std': IMAGENET_DEFAULT_STD,
        'first_conv': 'patch_embed.proj', 'classifier': 'head',
        **kwargs
    }


default_cfgs = {
    'vit_base_patch16_224': _cfg(
        url = "https://dl.fbaipublicfiles.com/dino/dino_vitbase16_pretrain/dino_vitbase16_pretrain.pth",
        mean=(0.485, 0.456, 0.406), std=(0.229, 0.224, 0.225),
    ),
}


class Mlp(nn.Module):
    def __init__(self, in_features, hidden_features=None, out_features=None, act_layer=nn.GELU, drop=0.):
        super().__init__()
        out_features = out_features or in_features
        hidden_features = hidden_features or in_features
        self.fc1 = nn.Linear(in_features, hidden_features)
        self.act = act_layer()
        self.fc2 = nn.Linear(hidden_features, out_features)
        self.drop = nn.Dropout(drop)

    def forward(self, x):
        x = self.fc1(x)
        x = self.act(x)
        x = self.drop(x)
        x = self.fc2(x)
        x = self.drop(x)
        return x


class Attention(nn.Module):
    def __init__(self, dim, num_heads=8, qkv_bias=False, qk_scale=None, attn_drop=0., proj_drop=0., with_qkv=True):
        super().__init__()
        self.num_heads = num_heads
        head_dim = dim // num_heads
        self.scale = qk_scale or head_dim ** -0.5
        self.with_qkv = with_qkv
        if self.with_qkv:
            self.qkv = nn.Linear(dim, dim * 3, bias=qkv_bias)
            self.proj = nn.Linear(dim, dim)
            self.proj_drop = nn.Dropout(proj_drop)
        self.attn_drop = nn.Dropout(attn_drop)

    def forward(self, x, return_attn=False):
        B, N, C = x.shape
        if self.with_qkv:
            qkv = self.qkv(x).reshape(B, N, 3, self.num_heads, C // self.num_heads).permute(2, 0, 3, 1, 4)
            q, k, v = qkv[0], qkv[1], qkv[2]
        else:
            qkv = x.reshape(B, N, self.num_heads, C // self.num_heads).permute(0, 2, 1, 3)
            q, k, v = qkv, qkv, qkv

        attn = (q @ k.transpose(-2, -1)) * self.scale
        attn = attn.softmax(dim=-1)
        attn = self.attn_drop(attn)

        x = (attn @ v).transpose(1, 2).reshape(B, N, C)
        if self.with_qkv:
            x = self.proj(x)
            x = self.proj_drop(x)
        if return_attn:
            return x, attn
        return x


class Block(nn.Module):

    def __init__(self, dim, num_heads, mlp_ratio=4., qkv_bias=False, qk_scale=None, drop=0., attn_drop=0.,
                 drop_path=0.1, act_layer=nn.GELU, norm_layer=nn.LayerNorm, attention_type='divided_space_time'):
        super().__init__()
        self.attention_type = attention_type
        self.class_tokens = 1
        assert (attention_type in ['divided_space_time', 'space_only', 'joint_space_time'])

        self.norm1 = norm_layer(dim)
        self.attn = Attention(
            dim, num_heads=num_heads, qkv_bias=qkv_bias, qk_scale=qk_scale, attn_drop=attn_drop, proj_drop=drop)

        # Temporal Attention Parameters
        if self.attention_type == 'divided_space_time':
            self.temporal_norm1 = norm_layer(dim)
            self.temporal_attn = Attention(
                dim, num_heads=num_heads, qkv_bias=qkv_bias, qk_scale=qk_scale, attn_drop=attn_drop, proj_drop=drop)
            self.temporal_fc = nn.Linear(dim, dim)

        # drop path
        self.drop_path = DropPath(drop_path) if drop_path > 0. else nn.Identity()
        self.norm2 = norm_layer(dim)
        mlp_hidden_dim = int(dim * mlp_ratio)
        self.mlp = Mlp(in_features=dim, hidden_features=mlp_hidden_dim, act_layer=act_layer, drop=drop)

    def forward(self, x, B, T, W, return_attn=False):
        num_spatial_tokens = (x.size(1) - self.class_tokens) // T
        H = num_spatial_tokens // W

        if self.attention_type in ['space_only', 'joint_space_time']:
            x = x + self.drop_path(self.attn(self.norm1(x)))
            x = x + self.drop_path(self.mlp(self.norm2(x)))
            return x
        elif self.attention_type == 'divided_space_time':
            # Temporal
            if self.class_tokens == 1:
                xt = x[:, 1:, :]
            else:
                xt = x[:, 1:-1, :]
            xt = rearrange(xt, 'b (h w t) m -> (b h w) t m', b=B, h=H, w=W, t=T)
            res_temporal = self.drop_path(self.temporal_attn(self.temporal_norm1(xt)))
            res_temporal = rearrange(res_temporal, '(b h w) t m -> b (h w t) m', b=B, h=H, w=W, t=T)
            res_temporal = self.temporal_fc(res_temporal)
            if self.class_tokens == 1:
                xt = x[:, 1:, :] + res_temporal
            else:
                xt = x[:, 1:-1, :] + res_temporal

            # Spatial
            init_cls_token = x[:, 0, :].unsqueeze(1)
            cls_token = init_cls_token.repeat(1, T, 1)
            cls_token = rearrange(cls_token, 'b t m -> (b t) m', b=B, t=T).unsqueeze(1)
            if self.class_tokens != 1:
                init_aux_cls_token = x[:, -1, :].unsqueeze(1)
                aux_cls_token = init_aux_cls_token.repeat(1, T, 1)
                aux_cls_token = rearrange(aux_cls_token, 'b t m -> (b t) m', b=B, t=T).unsqueeze(1)
            else:
                aux_cls_token = None
            xs = xt
            xs = rearrange(xs, 'b (h w t) m -> (b t) (h w) m', b=B, h=H, w=W, t=T)
            if aux_cls_token is None:
                xs = torch.cat((cls_token, xs), 1)
            else:
                xs = torch.cat((cls_token, xs, aux_cls_token), 1)
            if return_attn:
                _, attn = self.attn(self.norm1(xs), return_attn=return_attn)
                return attn
            else:
                res_spatial = self.drop_path(self.attn(self.norm1(xs)))

            # Taking care of CLS token
            cls_token = res_spatial[:, 0, :]
            cls_token = rearrange(cls_token, '(b t) m -> b t m', b=B, t=T)
            cls_token = torch.mean(cls_token, 1, True)  # averaging for every frame

            if aux_cls_token is not None:
                aux_cls_token = res_spatial[:, -1, :]
                aux_cls_token = rearrange(aux_cls_token, '(b t) m -> b t m', b=B, t=T)
                aux_cls_token = torch.mean(aux_cls_token, 1, True)  # averaging for every frame

            if aux_cls_token is None:
                res_spatial = res_spatial[:, 1:, :]
            else:
                res_spatial = res_spatial[:, 1:-1, :]
            res_spatial = rearrange(res_spatial, '(b t) (h w) m -> b (h w t) m', b=B, h=H, w=W, t=T)
            res = res_spatial
            x = xt

            # Mlp
            if aux_cls_token is None:
                x = torch.cat((init_cls_token, x), 1) + torch.cat((cls_token, res), 1)
            else:
                x = torch.cat((init_cls_token, x, init_aux_cls_token), 1) + torch.cat((cls_token, res, aux_cls_token), 1)
            x = x + self.drop_path(self.mlp(self.norm2(x)))
            return x


class PatchEmbed(nn.Module):
    """ Image to Patch Embedding
    """

    def __init__(self, img_size=224, patch_size=16, in_chans=3, embed_dim=768):
        super().__init__()
        img_size = to_2tuple(img_size)
        patch_size = to_2tuple(patch_size)
        num_patches = (img_size[1] // patch_size[1]) * (img_size[0] // patch_size[0])
        self.img_size = img_size
        self.patch_size = patch_size
        self.num_patches = num_patches

        self.proj = nn.Conv2d(in_chans, embed_dim, kernel_size=patch_size, stride=patch_size)

    def forward(self, x):
        B, C, T, H, W = x.shape
        x = rearrange(x, 'b c t h w -> (b t) c h w')
        x = self.proj(x)
        W = x.size(-1)
        x = x.flatten(2).transpose(1, 2)
        return x, T, W


class VisionTransformer(nn.Module):
    """ Vision Transformer"""

    def __init__(self, img_size=224, patch_size=16, in_chans=3, num_classes=1000, embed_dim=768, depth=12,
                 num_heads=12, mlp_ratio=4., qkv_bias=False, qk_scale=None, drop_rate=0., attn_drop_rate=0.,
                 drop_path_rate=0.1, hybrid_backbone=None, norm_layer=nn.LayerNorm, num_frames=8,
                 attention_type='divided_space_time', dropout=0.):
        super().__init__()
        self.attention_type = attention_type
        self.depth = depth
        self.dropout = nn.Dropout(dropout)
        self.num_classes = num_classes
        self.num_features = self.embed_dim = embed_dim  # num_features for consistency with other models
        self.patch_embed = PatchEmbed(
            img_size=img_size, patch_size=patch_size, in_chans=in_chans, embed_dim=embed_dim)
        num_patches = self.patch_embed.num_patches

        # Positional Embeddings
        self.cls_token = nn.Parameter(torch.zeros(1, 1, embed_dim))
        self.pos_embed = nn.Parameter(torch.zeros(1, num_patches + 1, embed_dim))
        self.pos_drop = nn.Dropout(p=drop_rate)
        if self.attention_type != 'space_only':
            self.time_embed = nn.Parameter(torch.zeros(1, num_frames, embed_dim))
            self.time_drop = nn.Dropout(p=drop_rate)

        # Attention Blocks
        dpr = [x.item() for x in torch.linspace(0, drop_path_rate, self.depth)]  # stochastic depth decay rule
        self.blocks = nn.ModuleList([
            Block(
                dim=embed_dim, num_heads=num_heads, mlp_ratio=mlp_ratio, qkv_bias=qkv_bias, qk_scale=qk_scale,
                drop=drop_rate, attn_drop=attn_drop_rate, drop_path=dpr[i], norm_layer=norm_layer,
                attention_type=self.attention_type)
            for i in range(self.depth)])
        self.norm = norm_layer(embed_dim)

        # Classifier head
        self.head = nn.Linear(embed_dim, num_classes) if num_classes > 0 else nn.Identity()

        trunc_normal_(self.pos_embed, std=.02)
        trunc_normal_(self.cls_token, std=.02)
        self.apply(self._init_weights)

        # initialization of temporal attention weights
        if self.attention_type == 'divided_space_time':
            i = 0
            for m in self.blocks.modules():
                m_str = str(m)
                if 'Block' in m_str:
                    if i > 0:
                        nn.init.constant_(m.temporal_fc.weight, 0)
                        nn.init.constant_(m.temporal_fc.bias, 0)
                    i += 1

    def _init_weights(self, m):
        if isinstance(m, nn.Linear):
            trunc_normal_(m.weight, std=.02)
            if isinstance(m, nn.Linear) and m.bias is not None:
                nn.init.constant_(m.bias, 0)
        elif isinstance(m, nn.LayerNorm):
            nn.init.constant_(m.bias, 0)
            nn.init.constant_(m.weight, 1.0)

    @torch.jit.ignore
    def no_weight_decay(self):
        return {'pos_embed', 'cls_token', 'time_embed'}

    def get_classifier(self):
        return self.head

    def reset_classifier(self, num_classes, global_pool=''):
        self.num_classes = num_classes
        self.head = nn.Linear(self.embed_dim, num_classes) if num_classes > 0 else nn.Identity()

    def forward_features(self, x, get_all=False, get_attn=False):
        B = x.shape[0]
        x, T, W = self.patch_embed(x)
        cls_tokens = self.cls_token.expand(x.size(0), -1, -1)
        x = torch.cat((cls_tokens, x), dim=1)

        # resizing the positional embeddings in case they don't match the input at inference
        if x.size(1) != self.pos_embed.size(1):
            # print(x.shape, self.pos_embed.shape)
            pos_embed = self.pos_embed
            cls_pos_embed = pos_embed[0, 0, :].unsqueeze(0).unsqueeze(1)
            other_pos_embed = pos_embed[0, 1:, :].unsqueeze(0).transpose(1, 2)
            P = int(other_pos_embed.size(2) ** 0.5)
            H = x.size(1) // W
            other_pos_embed = other_pos_embed.reshape(1, x.size(2), P, P)
            # print(other_pos_embed.shape, H, W)
            new_pos_embed = F.interpolate(other_pos_embed, size=(H, W), mode='nearest')
            new_pos_embed = new_pos_embed.flatten(2)
            new_pos_embed = new_pos_embed.transpose(1, 2)
            new_pos_embed = torch.cat((cls_pos_embed, new_pos_embed), 1)
            x = x + new_pos_embed
            # print(new_pos_embed.shape)
            # exit(0)
        else:
            x = x + self.pos_embed
        x = self.pos_drop(x)

        # Time Embeddings
        if self.attention_type != 'space_only':
            cls_tokens = x[:B, 0, :].unsqueeze(1)
            x = x[:, 1:]
            x = rearrange(x, '(b t) n m -> (b n) t m', b=B, t=T)
            # Resizing time embeddings in case they don't match
            if T != self.time_embed.size(1):
                time_embed = self.time_embed.transpose(1, 2)
                new_time_embed = F.interpolate(time_embed, size=(T), mode='nearest')
                new_time_embed = new_time_embed.transpose(1, 2)
                x = x + new_time_embed
            else:
                x = x + self.time_embed
            x = self.time_drop(x)
            x = rearrange(x, '(b n) t m -> b (n t) m', b=B, t=T)
            x = torch.cat((cls_tokens, x), dim=1)

        if get_attn:
            for i, blk in enumerate(self.blocks):
                if i < len(self.blocks) - 1:
                    x = blk(x, B, T, W)
                else:
                    # return attention of the last block
                    return blk(x, B, T, W, return_attn=True)

        # Attention blocks
        for blk in self.blocks:
            x = blk(x, B, T, W)

        # Predictions for space-only baseline
        if self.attention_type == 'space_only':
            x = rearrange(x, '(b t) n m -> b t n m', b=B, t=T)
            x = torch.mean(x, 1)  # averaging predictions for every frame

        x = self.norm(x)
        if get_all:
            return x
        return x[:, 0]

    def forward(self, x, use_head=False):
        x = self.forward_features(x)
        if use_head:
            x = self.head(x)
        return x

    def get_intermediate_layers(self, x, n=1):
        x = self.forward_features(x, get_all=True)
        return [x, ]

    def get_last_selfattention(self, x):
        x = self.prepare_tokens(x)
        for i, blk in enumerate(self.blocks):
            if i < len(self.blocks) - 1:
                x = blk(x)
            else:
                # return attention of the last block
                return blk(x, return_attention=True)


def _conv_filter(state_dict, patch_size=16):
    """ convert patch embedding weight from manual patchify + linear proj to conv"""
    out_dict = {}
    for k, v in state_dict.items():
        if 'patch_embed.proj.weight' in k:
            if v.shape[-1] != patch_size:
                patch_size = v.shape[-1]
            v = v.reshape((v.shape[0], 3, patch_size, patch_size))
        out_dict[k] = v
    return out_dict


class vit_base_patch16_224(nn.Module):
    def __init__(self, cfg, **kwargs):
        super(vit_base_patch16_224, self).__init__()
        self.pretrained = True
        patch_size = 16
        self.model = VisionTransformer(img_size=cfg.DATA.TRAIN_CROP_SIZE, num_classes=cfg.MODEL.NUM_CLASSES,
                                       patch_size=patch_size, embed_dim=768, depth=12, num_heads=12, mlp_ratio=4,
                                       qkv_bias=True, norm_layer=partial(nn.LayerNorm, eps=1e-6), drop_rate=0.,
                                       attn_drop_rate=0., drop_path_rate=0.1, num_frames=cfg.DATA.NUM_FRAMES,
                                       attention_type=cfg.TIMESFORMER.ATTENTION_TYPE, **kwargs)

        self.attention_type = cfg.TIMESFORMER.ATTENTION_TYPE
        self.model.default_cfg = default_cfgs['vit_base_patch16_224']
        self.num_patches = (cfg.DATA.TRAIN_CROP_SIZE // patch_size) * (cfg.DATA.TRAIN_CROP_SIZE // patch_size)
        pretrained_model = cfg.TIMESFORMER.PRETRAINED_MODEL
        if self.pretrained:
            load_pretrained(self.model, num_classes=self.model.num_classes, in_chans=kwargs.get('in_chans', 3),
                            filter_fn=_conv_filter, img_size=cfg.DATA.TRAIN_CROP_SIZE, num_patches=self.num_patches,
                            attention_type=self.attention_type, pretrained_model=pretrained_model)

    def forward(self, x):
        x = self.model(x)
        return x


class TimeSformer(nn.Module):
    def __init__(self, img_size=224, patch_size=16, num_classes=400, num_frames=8, attention_type='divided_space_time',
                 pretrained_model='', **kwargs):
        super(TimeSformer, self).__init__()
        self.pretrained = True
        self.model = VisionTransformer(img_size=img_size, num_classes=num_classes, patch_size=patch_size, embed_dim=768,
                                       depth=12, num_heads=12, mlp_ratio=4, qkv_bias=True,
                                       norm_layer=partial(nn.LayerNorm, eps=1e-6), drop_rate=0., attn_drop_rate=0.,
                                       drop_path_rate=0.1, num_frames=num_frames, attention_type=attention_type,
                                       **kwargs)

        self.attention_type = attention_type
        self.model.default_cfg = default_cfgs['vit_base_patch' + str(patch_size) + '_224']
        self.num_patches = (img_size // patch_size) * (img_size // patch_size)
        if self.pretrained:
            load_pretrained(self.model, num_classes=self.model.num_classes, in_chans=kwargs.get('in_chans', 3),
                            filter_fn=_conv_filter, img_size=img_size, num_frames=num_frames,
                            num_patches=self.num_patches, attention_type=self.attention_type,
                            pretrained_model=pretrained_model)

    def forward(self, x):
        x = self.model(x)
        return x


class FlowTokenVisionTransformer(VisionTransformer):
    def __init__(self, *args, img_size=224, patch_size=16, in_chans=3, embed_dim=768, **kwargs):
        super(FlowTokenVisionTransformer, self).__init__(*args, img_size=img_size, patch_size=patch_size,
                                                        in_chans=in_chans, embed_dim=embed_dim, **kwargs)

        self.aux_cls_token = nn.Parameter(torch.zeros(1, 1, embed_dim))
        self.n_cls_tokens = 2
        self.pos_embed = nn.Parameter(torch.zeros(1,  self.patch_embed.num_patches + self.n_cls_tokens, embed_dim))
        self.flow_patch_embed = PatchEmbed(
            img_size=img_size, patch_size=patch_size, in_chans=in_chans, embed_dim=embed_dim)
        for blk in self.blocks:
            blk.class_tokens = 2

    def forward_features(self, x, get_all=False, is_flow=False):
        B = x.shape[0]
        if is_flow:
            x, T, W = self.flow_patch_embed(x)
        else:
            x, T, W = self.patch_embed(x)
        cls_tokens = self.cls_token.expand(x.size(0), -1, -1)
        aux_cls_tokens = self.aux_cls_token.expand(x.size(0), -1, -1)
        x = torch.cat((cls_tokens, x, aux_cls_tokens), dim=1)

        # resizing the positional embeddings in case they don't match the input at inference
        if x.size(1) != self.pos_embed.size(1):
            pos_embed = self.pos_embed
            cls_pos_embed = pos_embed[0, 0, :].unsqueeze(0).unsqueeze(1)
            aux_pos_embed = pos_embed[0, -1, :].unsqueeze(0).unsqueeze(1)
            other_pos_embed = pos_embed[0, 1:-1, :].unsqueeze(0).transpose(1, 2)
            P = int(other_pos_embed.size(2) ** 0.5)
            H = x.size(1) // W
            other_pos_embed = other_pos_embed.reshape(1, x.size(2), P, P)
            new_pos_embed = F.interpolate(other_pos_embed, size=(H, W), mode='nearest')
            new_pos_embed = new_pos_embed.flatten(2)
            new_pos_embed = new_pos_embed.transpose(1, 2)
            new_pos_embed = torch.cat((cls_pos_embed, new_pos_embed, aux_pos_embed), 1)
            x = x + new_pos_embed
        else:
            x = x + self.pos_embed
        x = self.pos_drop(x)

        # Time Embeddings
        if self.attention_type != 'space_only':
            cls_tokens = x[:B, 0, :].unsqueeze(1)
            aux_cls_tokens = x[:B, -1, :].unsqueeze(1)
            x = x[:, 1:-1]
            x = rearrange(x, '(b t) n m -> (b n) t m', b=B, t=T)
            # Resizing time embeddings in case they don't match
            if T != self.time_embed.size(1):
                time_embed = self.time_embed.transpose(1, 2)
                new_time_embed = F.interpolate(time_embed, size=(T), mode='nearest')
                new_time_embed = new_time_embed.transpose(1, 2)
                x = x + new_time_embed
            else:
                x = x + self.time_embed
            x = self.time_drop(x)
            x = rearrange(x, '(b n) t m -> b (n t) m', b=B, t=T)
            x = torch.cat((cls_tokens, x, aux_cls_tokens), dim=1)

        # Attention blocks
        for blk in self.blocks:
            x = blk(x, B, T, W)

        # Predictions for space-only baseline
        if self.attention_type == 'space_only':
            x = rearrange(x, '(b t) n m -> b t n m', b=B, t=T)
            x = torch.mean(x, 1)  # averaging predictions for every frame

        x = self.norm(x)
        if get_all:
            return x

        if not self.training:
            return torch.cat((x[:, 0], x[:, -1]), dim=1)
        if is_flow:
            return x[:, -1]  # aux class token
        else:
            return x[:, 0]  # class token

    def forward(self, x, use_head=False, is_flow=False):
        x = self.forward_features(x, is_flow=is_flow)
        if use_head:
            x = self.head(x)
        return x


class AuxTokenVisionTransformer(VisionTransformer):
    def __init__(self, *args, img_size=224, patch_size=16, in_chans=3, embed_dim=768, **kwargs):
        super(AuxTokenVisionTransformer, self).__init__(*args, img_size=img_size, patch_size=patch_size,
                                                        in_chans=in_chans, embed_dim=embed_dim, **kwargs)
        self.aux_cls_token = nn.Parameter(torch.zeros(1, 1, embed_dim))
        self.n_cls_tokens = 2
        self.pos_embed = nn.Parameter(torch.zeros(1,  self.patch_embed.num_patches + self.n_cls_tokens, embed_dim))
        for blk in self.blocks:
            blk.class_tokens = 2

    def forward_features(self, x, get_all=False):
        B = x.shape[0]
        x, T, W = self.patch_embed(x)
        cls_tokens = self.cls_token.expand(x.size(0), -1, -1)
        aux_cls_tokens = self.aux_cls_token.expand(x.size(0), -1, -1)
        x = torch.cat((cls_tokens, x, aux_cls_tokens), dim=1)

        # resizing the positional embeddings in case they don't match the input at inference
        if x.size(1) != self.pos_embed.size(1):
            pos_embed = self.pos_embed
            cls_pos_embed = pos_embed[0, 0, :].unsqueeze(0).unsqueeze(1)
            aux_pos_embed = pos_embed[0, -1, :].unsqueeze(0).unsqueeze(1)
            other_pos_embed = pos_embed[0, 1:-1, :].unsqueeze(0).transpose(1, 2)
            P = int(other_pos_embed.size(2) ** 0.5)
            H = x.size(1) // W
            other_pos_embed = other_pos_embed.reshape(1, x.size(2), P, P)
            new_pos_embed = F.interpolate(other_pos_embed, size=(H, W), mode='nearest')
            new_pos_embed = new_pos_embed.flatten(2)
            new_pos_embed = new_pos_embed.transpose(1, 2)
            new_pos_embed = torch.cat((cls_pos_embed, new_pos_embed, aux_pos_embed), 1)
            x = x + new_pos_embed
        else:
            x = x + self.pos_embed
        x = self.pos_drop(x)

        # Time Embeddings
        if self.attention_type != 'space_only':
            cls_tokens = x[:B, 0, :].unsqueeze(1)
            aux_cls_tokens = x[:B, -1, :].unsqueeze(1)
            x = x[:, 1:-1]
            x = rearrange(x, '(b t) n m -> (b n) t m', b=B, t=T)
            # Resizing time embeddings in case they don't match
            if T != self.time_embed.size(1):
                time_embed = self.time_embed.transpose(1, 2)
                new_time_embed = F.interpolate(time_embed, size=(T), mode='nearest')
                new_time_embed = new_time_embed.transpose(1, 2)
                x = x + new_time_embed
            else:
                x = x + self.time_embed
            x = self.time_drop(x)
            x = rearrange(x, '(b n) t m -> b (n t) m', b=B, t=T)
            x = torch.cat((cls_tokens, x, aux_cls_tokens), dim=1)

        # Attention blocks
        for blk in self.blocks:
            x = blk(x, B, T, W)

        # Predictions for space-only baseline
        if self.attention_type == 'space_only':
            x = rearrange(x, '(b t) n m -> b t n m', b=B, t=T)
            x = torch.mean(x, 1)  # averaging predictions for every frame

        x = self.norm(x)
        if get_all:
            return x

        if not self.training:
            return torch.cat((x[:, 0], x[:, -1]), dim=1)
        return x[:, 0], x[:, -1]  # class token, aux class token

    def forward(self, x, use_head=False):
        x = self.forward_features(x)
        if use_head:
            x = self.head(x)
        return x


def get_vit_base_patch16_224(cfg, no_head=False, **kwargs):
    patch_size = 16
    vit = VisionTransformer(img_size=cfg.DATA.TRAIN_CROP_SIZE, num_classes=cfg.MODEL.NUM_CLASSES,
                            patch_size=patch_size, embed_dim=768, depth=12, num_heads=12, mlp_ratio=4,
                            qkv_bias=True, norm_layer=partial(nn.LayerNorm, eps=1e-6), drop_rate=0.,
                            attn_drop_rate=0., drop_path_rate=0.1, num_frames=cfg.DATA.NUM_FRAMES,
                            attention_type=cfg.TIMESFORMER.ATTENTION_TYPE, **kwargs)
    vit.attention_type = cfg.TIMESFORMER.ATTENTION_TYPE
    vit.default_cfg = default_cfgs['vit_base_patch16_224']
    vit.num_patches = (cfg.DATA.TRAIN_CROP_SIZE // patch_size) * (cfg.DATA.TRAIN_CROP_SIZE // patch_size)
    pretrained_model = cfg.TIMESFORMER.PRETRAINED_MODEL
    if pretrained_model:
        load_pretrained(vit, num_classes=vit.num_classes, in_chans=kwargs.get('in_chans', 3),
                        filter_fn=_conv_filter, img_size=cfg.DATA.TRAIN_CROP_SIZE, num_patches=vit.num_patches,
                        attention_type=vit.attention_type, pretrained_model=pretrained_model)
    if no_head:
        vit.head = None
    return vit


def get_endomamba_small(cfg, no_head=False, pretrained=True, num_classes=2, **kwargs):
    model = endomamba_small(pretrained=pretrained, num_classes=num_classes, with_head=not no_head, **kwargs)
    return model


def get_aux_token_vit(cfg, no_head=False, **kwargs):
    patch_size = 16
    vit = AuxTokenVisionTransformer(img_size=cfg.DATA.TRAIN_CROP_SIZE, num_classes=cfg.MODEL.NUM_CLASSES,
                            patch_size=patch_size, embed_dim=768, depth=12, num_heads=12, mlp_ratio=4,
                            qkv_bias=True, norm_layer=partial(nn.LayerNorm, eps=1e-6), drop_rate=0.,
                            attn_drop_rate=0., drop_path_rate=0.1, num_frames=cfg.DATA.NUM_FRAMES,
                            attention_type=cfg.TIMESFORMER.ATTENTION_TYPE, **kwargs)
    vit.attention_type = cfg.TIMESFORMER.ATTENTION_TYPE
    vit.default_cfg = default_cfgs['vit_base_patch16_224']
    vit.num_patches = (cfg.DATA.TRAIN_CROP_SIZE // patch_size) * (cfg.DATA.TRAIN_CROP_SIZE // patch_size)
    pretrained_model = cfg.TIMESFORMER.PRETRAINED_MODEL
    load_pretrained(vit, num_classes=vit.num_classes, in_chans=kwargs.get('in_chans', 3),
                    filter_fn=_conv_filter, img_size=cfg.DATA.TRAIN_CROP_SIZE, num_patches=vit.num_patches+1,
                    attention_type=vit.attention_type, pretrained_model=pretrained_model)
    if no_head:
        vit.head = None
    return vit


def build_vit_base_patch16_224():
    from utils.parser import parse_args, load_config

    opt = parse_args()
    opt.cfg_file = "models/configs/Kinetics/TimeSformer_divST_8x32_224.yaml"
    config = load_config(opt)
    model = get_vit_base_patch16_224(cfg=config)

    return model

if __name__ == '__main__':
    from utils.parser import parse_args, load_config

    opt = parse_args()
    opt.cfg_file = "models/configs/Kinetics/TimeSformer_divST_8x32_224.yaml"
    config = load_config(opt)
    model = get_vit_base_patch16_224(cfg=config)
    # model = get_aux_token_vit(cfg=config)

    flops, params = profile(model, input_size=(1, 3, 8, 224, 224))
    print('FLOPs = ' + str(flops / 1000 ** 3) + 'G')
    print('Params = ' + str(params / 1000 ** 2) + 'M')
    exit(0)

    sample = torch.ones((2, 3, 8, 224, 224))
    out1 = model(sample)
    # out2 = model(sample, is_flow=True)
    out2 = torch.ones_like(out1)
    print(out1.shape, out2.shape)
    loss = torch.sum(out1 + out2)
    loss.backward()

    # ckpt = torch.load("/home/kanchanaranasinghe/repo/dino/checkpoints/dino_b_02/checkpoint0040.pth")
    # renamed_checkpoint = {x[len("backbone."):]: y for x, y in ckpt['teacher'].items() if x.startswith("backbone.")}
    # msg = model.load_state_dict(renamed_checkpoint, strict=False)
    # print(f"Loaded model with msg: {msg}")
