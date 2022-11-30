import torch
import torch.nn as nn

class SelfAttention(nn.Module):
    def __init__(self, embed_size: int, heads: int) -> None:
        super().__init__()
        self.embed_size = embed_size
        self.heads = heads
        self.head_dim = embed_size // heads

        assert (self.head_dim * self.heads == self.embed_size), "Embed size needs to be div by heads"

        self.values = nn.Linear(self.head_dim, self.head_dim, bias=False)
        self.keys = nn.Linear(self.head_dim, self.head_dim, bias=False)
        self.queries = nn.Linear(self.head_dim, self.head_dim, bias=False)
        self.fc_out = nn.Linear(self.heads*self.head_dim, self.embed_size)

    def forward(self, values: torch.tensor, keys: torch.tensor, queries: torch.tensor, mask: torch.tensor):
        N = queries.shape[0]
        value_len, key_len, query_len = values.shape[1], keys.shape[1], queries.shape[1]

        # Split embedding into self.heads pieces
        values = values.reshape(N, value_len, self.heads, self.head_dim)
        keys = keys.reshape(N, key_len, self.heads, self.head_dim)
        queries = queries.reshape(N, query_len, self.heads, self.head_dim)

        values = self.values(values)
        keys = self.keys(keys)
        queries = self.queries(queries)

        energy = torch.einsum("nqhd, nkhd -> nhqk", [queries, keys])
        # queries shape: (N, query_len, heads, heads_dim)
        # keys shape: (N, key_len, heads, heads_dim)
        # energy shape: (N, heads, query_len, key_len)

        if mask is not None:
            energy = energy.masked_fill(mask == 0, float("-1e20"))
 
        attention = torch.softmax(energy / (self.embed_size**(1/2)), dim=3)

        out = torch.einsum("nhql, nlhd -> nqhd", [attention, values])
        # attention shape: (N, heads, query_len, key_len)
        # values shape: (N, value_len, heads, heads_dim)
        # out shape: (N, query_len, heads, head_dim)

        out = out.reshape(N, query_len, self.heads*self.head_dim)
        out = self.fc_out(out)
        return out

class TransformerBlock(nn.Module):
    def __init__(self, embed_size: int, heads: int, dropout: float, forward_expansion: int) -> None:
        super().__init__()
        self.attention = SelfAttention(embed_size=embed_size, heads=heads)
        self.norm1 = nn.LayerNorm(embed_size)
        self.norm2 = nn.LayerNorm(embed_size)

        self.feed_forward = nn.Sequential(
                                        nn.Linear(embed_size, forward_expansion*embed_size),
                                        nn.ReLU(),
                                        nn.Linear(forward_expansion*embed_size, embed_size)
                                        )
        self.dropout = nn.Dropout(p=dropout)

    def forward(self, values: torch.tensor, keys: torch.tensor, queries: torch.tensor, mask: bool):
        attention = self.attention(values, keys, queries, mask)
        
        x = self.dropout(self.norm1(attention + queries))
        forward = self.feed_forward(x)
        out = self.dropout(self.norm2(forward + x))
        return out 

class Encoder(nn.Module):
    def __init__(self,
                src_vocab_size: int,
                embed_size: int,
                num_layers: int,
                heads: int,
                device,
                forward_expansion: int,
                dropout: float,
                max_length: int           # about position embedding
                ) -> None:
        super().__init__()
        self.embed_size = embed_size
        self.device = device
        self.word_embedding = nn.Embedding(src_vocab_size, embed_size)
        self.position_embedding = nn.Embedding(max_length, embed_size)

        self.layers = nn.ModuleList(
            [
                TransformerBlock(embed_size=embed_size, heads=heads, dropout=dropout, forward_expansion=forward_expansion)
                for _ in range(num_layers)
            ]
        )
        self.dropout = nn.Dropout(dropout)

    def forward(self, x: torch.tensor, mask: torch.tensor):
        N, seq_length = x.shape
        positions = torch.arange(0, seq_length).expand(N, seq_length).to(self.device)
        
        out = self.dropout(self.word_embedding(x) + self.position_embedding(positions))

        for layer in self.layers:
            out = layer(out, out, out, mask)
        return out

class DecoderBLock(nn.Module):
    def __init__(self, embed_size: int, heads: int, forward_expansion: int, dropout: float, device) -> None:
        super().__init__()
        self.attention = SelfAttention(embed_size, heads)
        self.norm = nn. LayerNorm(embed_size)
        self.transformer_block = TransformerBlock(embed_size, heads, dropout, forward_expansion)
        self.dropout = nn.Dropout(dropout)

    def forward(self, x, values, keys, src_mask, trg_mask):
        attention = self.attention(x, x, x, trg_mask)
        queries = self.dropout(self.norm(attention + x))
        out = self.transformer_block(values, keys, queries, src_mask)
        return out

class Decoder(nn.Module):
    def __init__(self, 
                trg_vocab_size: int,
                embed_size: int,
                num_layers: int,
                heads: int,
                forward_expansion: int,
                dropout: float,
                device,
                max_length: int
                ) -> None:
        super().__init__()
        self.device= device
        self.word_embedding = nn.Embedding(trg_vocab_size, embed_size)
        self.position_embedding = nn.Embedding(max_length, embed_size)
        self.layers = nn.ModuleList(
            [
                DecoderBLock(embed_size=embed_size, heads=heads, forward_expansion=forward_expansion, dropout=dropout, device=device)
                for _ in range(num_layers)
            ]
        )
        self.fc_out = nn.Linear(embed_size, trg_vocab_size)
        self.dropout = nn.Dropout(dropout)

    def forward(self, x, enc_out, src_mask, trg_mask):
        N, seq_length = x.shape
        positions = torch.arange(0, seq_length).expand(N, seq_length).to(self.device)
        x = self.dropout((self.word_embedding(x) + self.position_embedding(positions)))

        for layer in self.layers:
            x = layer(x, enc_out, enc_out, src_mask, trg_mask)
        
        out = self.fc_out(x)
        return out

class Transformer(nn.Module):
    def __init__(self,
                src_vocab_size: int,
                trg_vocab_size: int,
                src_pad_idx,
                trg_pad_idx,
                embed_size: int = 256,
                num_layers: int = 6,
                forward_expansion: int = 4,
                heads: int = 8,
                dropout: float = 0,
                device = "cuda",
                max_length: int = 100) -> None:
        super().__init__()

        self.encoder = Encoder(
                                src_vocab_size,
                                embed_size,
                                num_layers,
                                heads,
                                device,
                                forward_expansion,
                                dropout,
                                max_length
                                )
        self.decoder = Decoder(
                                trg_vocab_size,
                                embed_size,
                                num_layers,
                                heads,
                                forward_expansion,
                                dropout,
                                device,
                                max_length
                                )
        self.src_pad_idx = src_pad_idx
        self.trg_pad_idx = trg_pad_idx
        self.device= device

    def make_src_mask(self, src):
        src_mask = (src != self.src_pad_idx).unsqueeze(1).unsqueeze(2)
        # (N, 1, 1, src_len)
        return src_mask.to(self.device)

    def make_trg_mask(self, trg):
        N, trg_len = trg.shape
        trg_mask = torch.tril(torch.ones((trg_len, trg_len))).expand(N, 1, trg_len, trg_len)
        return trg_mask.to(self.device)

    def forward(self, src, trg):
        src_mask = self.make_src_mask(src)
        trg_mask = self.make_trg_mask(trg)
        enc_src = self.encoder(src, src_mask)
        out = self.decoder(trg, enc_src, src_mask, trg_mask)
        return out

if __name__ == "__main__":
    device = torch.device("mps")
    print(device)

    x = torch.tensor([[1, 5, 6, 4, 3, 9, 5, 2, 0], [1, 8, 7, 3, 4, 5, 6, 7, 2]]).to(
        device
    )
    trg = torch.tensor([[1, 7, 4, 3, 5, 9, 2, 0], [1, 5, 6, 2, 4, 7, 6, 2]]).to(device)

    src_pad_idx = 0
    trg_pad_idx = 0
    src_vocab_size = 10
    trg_vocab_size = 10
    model = Transformer(src_vocab_size, trg_vocab_size, src_pad_idx, trg_pad_idx, device=device).to(
        device
    )
    out = model(x, trg[:, :-1])
    print(out.shape)