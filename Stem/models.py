import torch
import torch.nn as nn
import numpy as np
import math
from timm.models.vision_transformer import PatchEmbed, Attention, Mlp


def modulate(x, shift, scale):
    return x * (1 + scale.unsqueeze(1)) + shift.unsqueeze(1)


#################################################################################
#               Embedding Layers for Timesteps and Class Labels                 #
#################################################################################

class TimestepEmbedder(nn.Module):
    """
    Embeds scalar timesteps into vector representations.
    """
    def __init__(self, hidden_size, frequency_embedding_size=256):
        super().__init__()
        self.mlp = nn.Sequential(
            nn.Linear(frequency_embedding_size, hidden_size, bias=True),
            nn.SiLU(),
            nn.Linear(hidden_size, hidden_size, bias=True),
        )
        self.frequency_embedding_size = frequency_embedding_size

    @staticmethod
    def timestep_embedding(t, dim, max_period=10000):
        """
        Create sinusoidal timestep embeddings.
        :param t: a 1-D Tensor of N indices, one per batch element.
                          These may be fractional.
        :param dim: the dimension of the output.
        :param max_period: controls the minimum frequency of the embeddings.
        :return: an (N, D) Tensor of positional embeddings.
        """
        # https://github.com/openai/glide-text2im/blob/main/glide_text2im/nn.py
        half = dim // 2
        freqs = torch.exp(
            -math.log(max_period) * torch.arange(start=0, end=half, dtype=torch.float32) / half
        ).to(device=t.device)
        args = t[:, None].float() * freqs[None]
        embedding = torch.cat([torch.cos(args), torch.sin(args)], dim=-1)
        if dim % 2:
            embedding = torch.cat([embedding, torch.zeros_like(embedding[:, :1])], dim=-1)
        return embedding

    def forward(self, t):
        t_freq = self.timestep_embedding(t, self.frequency_embedding_size)
        t_emb = self.mlp(t_freq)
        return t_emb


class LabelEmbedder(nn.Module):
    """
    Embeds class labels into vector representations. Also handles label dropout for classifier-free guidance.
    """
    def __init__(self, num_classes, hidden_size, dropout_prob):
        super().__init__()
        use_cfg_embedding = dropout_prob > 0
        self.embedding_table = nn.Embedding(num_classes + use_cfg_embedding, hidden_size)
        self.num_classes = num_classes
        self.dropout_prob = dropout_prob

    def token_drop(self, labels, force_drop_ids=None):
        """
        Drops labels to enable classifier-free guidance.
        """
        if force_drop_ids is None:
            drop_ids = torch.rand(labels.shape[0], device=labels.device) < self.dropout_prob
        else:
            drop_ids = force_drop_ids == 1
        labels = torch.where(drop_ids, self.num_classes, labels)
        return labels

    def forward(self, labels, train, force_drop_ids=None):
        use_dropout = self.dropout_prob > 0
        if (train and use_dropout) or (force_drop_ids is not None):
            labels = self.token_drop(labels, force_drop_ids)
        embeddings = self.embedding_table(labels)
        return embeddings


class GeneJointEmbedding(nn.Module):
    def __init__(self, input_size, hidden_dim):
        """
        input_size: number of genes in input
        hidden_dim: num hidden dimension
        """
        super().__init__()
        self.gene_name_ebd = nn.Parameter(torch.empty((input_size, hidden_dim)),requires_grad=True) # trainable look-up table to encode gene name
        torch.nn.init.kaiming_uniform_(self.gene_name_ebd, a=math.sqrt(5))
        self.gene_count_ebd = nn.Sequential(
            nn.Linear(1, hidden_dim, bias=True),
            nn.SiLU(),
            nn.Linear(hidden_dim, hidden_dim, bias=True),            
        )
        torch.nn.init.xavier_uniform_(self.gene_count_ebd[0].weight)
        torch.nn.init.xavier_uniform_(self.gene_count_ebd[2].weight)

        self.hidden_dim = hidden_dim

    def forward(self, x):
        """
        x: (N, NumGene) tensor of inputs

        """
        gene_count_ebd = self.gene_count_ebd(x.squeeze(1).unsqueeze(2))      # (N, NumGene, hidden_dim)
        gene_name_ebd = self.gene_name_ebd                                   # (NumGene, hidden_dim)
        gene_joint_ebd = torch.add(gene_count_ebd, gene_name_ebd)            # (N, NumGene, hidden_dim)
        return gene_joint_ebd                                                # (N, NumGene, hidden_dim)


#################################################################################
#                                 Core Model                                    #
#################################################################################

class DiTBlock(nn.Module):
    """
    A DiT block with adaptive layer norm zero (adaLN-Zero) conditioning.
    """
    def __init__(self, hidden_size, num_heads, mlp_ratio=4.0, **block_kwargs):
        super().__init__()
        self.norm1 = nn.LayerNorm(hidden_size, elementwise_affine=False, eps=1e-6)
        self.attn = Attention(hidden_size, num_heads=num_heads, qkv_bias=True, **block_kwargs)
        self.norm2 = nn.LayerNorm(hidden_size, elementwise_affine=False, eps=1e-6)
        mlp_hidden_dim = int(hidden_size * mlp_ratio)
        approx_gelu = lambda: nn.GELU(approximate="tanh")
        self.mlp = Mlp(in_features=hidden_size, 
                       hidden_features=mlp_hidden_dim, 
                       act_layer=approx_gelu, drop=0)
        self.adaLN_modulation = nn.Sequential(
            nn.SiLU(),
            nn.Linear(hidden_size, 6 * hidden_size, bias=True)
        )

    def forward(self, x, c):
        shift_msa, scale_msa, gate_msa, shift_mlp, scale_mlp, gate_mlp = self.adaLN_modulation(c).chunk(6, dim=1)
        x = x + gate_msa.unsqueeze(1) * self.attn(modulate(self.norm1(x), shift_msa, scale_msa))
        x = x + gate_mlp.unsqueeze(1) * self.mlp(modulate(self.norm2(x), shift_mlp, scale_mlp))
        return x


class FinalLayer(nn.Module):
    """
    The final layer of DiT.
    """
    def __init__(self, hidden_size):
        super().__init__()
        self.norm_final = nn.LayerNorm(hidden_size, elementwise_affine=False, eps=1e-6)
        self.linear = nn.Linear(hidden_size, 2, bias=True)
        self.adaLN_modulation = nn.Sequential(
            nn.SiLU(),
            nn.Linear(hidden_size, 2 * hidden_size, bias=True)
        )

    def forward(self, x, c):
        shift, scale = self.adaLN_modulation(c).chunk(2, dim=1)
        x = modulate(self.norm_final(x), shift, scale)
        x = torch.permute(self.linear(x), (0, 2, 1))  # (N, NumGene, hidden_size) -> (N, 2, NumGene)
        return x


class StemModel(nn.Module):
    def __init__(self, 
        input_size=200,
        hidden_size=1152,
        depth=28,
        num_heads=16,
        mlp_ratio=4.0,
        label_size=512,
        learn_sigma=True,
    ):
        super().__init__()

        self.learn_sigma = learn_sigma
        self.input_size = input_size
        self.hidden_size = hidden_size
        self.num_heads = num_heads
        
        # gene name + gene count joint embedding
        self.gene_joint_embed = GeneJointEmbedding(self.input_size, self.hidden_size)
        # time step embedding
        self.time_embed = TimestepEmbedder(self.hidden_size)
        # label embedding (input label is already in embedding form, here just reorganize the size using linear layer)
        self.label_embed = nn.Sequential(
            nn.Linear(label_size, label_size, bias=True),
            nn.SiLU(),
            nn.Linear(label_size, hidden_size, bias=True),
        )

        # no positional embedding

        self.blocks = nn.ModuleList([
            DiTBlock(hidden_size, num_heads, mlp_ratio=mlp_ratio) for _ in range(depth)
        ])
        
        self.final_layer = FinalLayer(self.hidden_size)
        self.initialize_weights()

    def initialize_weights(self):
        # Initialize transformer layers:
        def _basic_init(module):
            if isinstance(module, nn.Linear):
                torch.nn.init.xavier_uniform_(module.weight)
                if module.bias is not None:
                    nn.init.constant_(module.bias, 0)
        self.apply(_basic_init)

        # Initialize timestep embedding MLP:
        nn.init.normal_(self.time_embed.mlp[0].weight, std=0.02)
        nn.init.normal_(self.time_embed.mlp[2].weight, std=0.02)
        nn.init.normal_(self.label_embed[0].weight, std=0.02)
        nn.init.normal_(self.label_embed[2].weight, std=0.02)

        # Zero-out adaLN modulation layers in DiT blocks:
        for block in self.blocks:
            nn.init.constant_(block.adaLN_modulation[-1].weight, 0)
            nn.init.constant_(block.adaLN_modulation[-1].bias, 0)

        # Zero-out output layers:
        nn.init.constant_(self.final_layer.adaLN_modulation[-1].weight, 0)
        nn.init.constant_(self.final_layer.adaLN_modulation[-1].bias, 0)
        nn.init.constant_(self.final_layer.linear.weight, 0)
        nn.init.constant_(self.final_layer.linear.bias, 0)


    def forward(self, x, t, y):
        """
        Forward pass of DiT.
        x: (N, NumGene) tensor of inputs
        t: (N,) tensor of diffusion timesteps
        y: (N, 512) tensor of conditions
        """
        x = self.gene_joint_embed(x)             # (N, NumGene, hidden_dim) [gene_joint_ebd]
        t = self.time_embed(t)                   # (N, hidden_dim) [time_ebd]
        y = self.label_embed(y)                  # (N, hidden_dim) [label_ebd]
        c = t + y                                # (N, hidden_dim) [condition]
        for block in self.blocks:
            x = block(x, c)                      # (N, NumGene, hidden_dim)
        x = self.final_layer(x, c)               # (N, 2, NumGene)
        return x


def Stem(**kwargs):
    return StemModel(**kwargs)

Stem_models = {"Stem": Stem}
# could set models with different sizes as default options in dictionary "Stem_models". 
