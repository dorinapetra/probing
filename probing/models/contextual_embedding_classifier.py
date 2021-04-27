#! /usr/bin/env python
# -*- coding: utf-8 -*-
# vim:fenc=utf-8
#
# Copyright © 2019 Judit Acs <judit@sch.bme.hu>
#
# Distributed under terms of the MIT license.
import torch
import torch.nn as nn
import numpy as np
import logging
from transformers import AutoModel, AutoConfig

from probing.models.base import BaseModel
from probing.models.mlp import MLP

use_cuda = torch.cuda.is_available()


def to_cuda(var):
    if use_cuda:
        return var.cuda()
    return var


class Embedder(nn.Module):
    def __init__(self, model_name, layer_pooling,
                 randomize_embedding_weights=False):
        super().__init__()
        global_key = (f'{model_name}_model', randomize_embedding_weights)
        if global_key in globals():
            self.embedder = globals()[global_key]
        else:
            self.config = AutoConfig.from_pretrained(
                model_name, output_hidden_states=True)
            if randomize_embedding_weights:
                logging.info(f"Loading {model_name} with random weights.")
                self.embedder = AutoModel.from_config(self.config)
            else:
                logging.info(f"Loading {model_name}.")
                self.embedder = AutoModel.from_pretrained(
                    model_name, config=self.config)
            globals()[global_key] = self.embedder
            for p in self.embedder.parameters():
                p.requires_grad = False
        self.get_sizes()
        try:
            layer_pooling = int(layer_pooling)
        except ValueError:
            pass
        self.layer_pooling = layer_pooling
        if self.layer_pooling == 'weighted_sum':
            self.weights = nn.Parameter(
                torch.ones(self.n_layer, dtype=torch.float))
            self.softmax = nn.Softmax(0)

    def forward(self, sentences, sentence_lens):
        self.embedder.train(False)
        with torch.no_grad():
            mask = torch.arange(sentences.size(1)) < \
                    torch.LongTensor(sentence_lens).unsqueeze(1)
            mask = to_cuda(mask.long())
            out = self.embedder(sentences, attention_mask=mask)
            return out[-1]

    def embed(self, sentences, sentence_lens):
        out = self.forward(sentences, sentence_lens)
        if self.layer_pooling == 'weighted_sum':
            w = self.softmax(self.weights)
            return (w[:, None, None, None] * torch.stack(out)).sum(0).detach()
        if self.layer_pooling == 'all':
            return torch.stack(out)
        if self.layer_pooling == 'sum':
            return torch.sum(torch.stack(out), axis=0)
        if self.layer_pooling == 'last':
            return out[-1]
        if self.layer_pooling == 'first':
            return out[0]
        if isinstance(self.layer_pooling, int):
            return out[self.layer_pooling]
        raise ValueError(f"Unknown pooling mechanism: {self.layer_pooling}")

    def get_sizes(self):
        with torch.no_grad():
            d = self.embedder.dummy_inputs
            if next(self.parameters()).is_cuda:
                for param in d:
                    if isinstance(d[param], torch.Tensor):
                        d[param] = d[param].cuda()
            out = self.embedder(**d)[-1]
            self.n_layer = len(out)
            self.hidden_size = out[0].size(-1)

    def state_dict(self, *args, **kwargs):
        if self.layer_pooling == 'weighted_sum':
            args[0]['{}weights'.format(args[1])] = self.weights
        return args[0]


class SentenceRepresentationProber(BaseModel):
    def __init__(self, config, dataset):
        super().__init__(config)
        self.dataset = dataset
        randweights = self.config.randomize_embedding_weights
        self.embedder = Embedder(self.config.model_name,
                                 layer_pooling='all',
                                 randomize_embedding_weights=randweights)
        self.output_size = len(dataset.vocabs.label)
        self.dropout = nn.Dropout(self.config.dropout)
        self.criterion = nn.CrossEntropyLoss()

        mlp_input_size = self.embedder.hidden_size
        if self.config.subword_pooling == 'f+l':
            self.subword_w = nn.Parameter(torch.ones(1, dtype=torch.float) / 2)
        elif self.config.subword_pooling == 'lstm':
            sw_lstm_size = getattr(self.config, 'subword_lstm_size',
                                   self.embedder.hidden_size)
            mlp_input_size = sw_lstm_size
            self.pool_lstm = nn.LSTM(
                self.embedder.hidden_size,
                sw_lstm_size // 2,
                num_layers=1,
                batch_first=True,
                bidirectional=True,
            )
        elif self.config.subword_pooling == 'attn':
            self.subword_mlp = MLP(
                self.embedder.hidden_size,
                layers=[self.config.subword_mlp_size],
                nonlinearity='ReLU',
                output_size=1
            )
            self.softmax = nn.Softmax(dim=0)
        elif self.config.subword_pooling == 'last2':
            mlp_input_size *= 2
        self.mlp = MLP(
            input_size=mlp_input_size,
            layers=self.config.mlp_layers,
            nonlinearity=self.config.mlp_nonlinearity,
            output_size=self.output_size,
        )
        self.pooling_func = {
            'first': self._forward_first_last,
            'last': self._forward_first_last,
            'max': self._forward_elementwise_pool,
            'sum': self._forward_elementwise_pool,
            'avg': self._forward_elementwise_pool,
            'last2': self._forward_last2,
            'f+l': self._forward_first_plus_last,
            'lstm': self._forward_lstm,
            'attn': self._forward_mlp,
        }
        self.layer_pooling = config.layer_pooling
        if self.layer_pooling == 'weighted_sum':
            self.weights = nn.Parameter(
                torch.ones(self.embedder.n_layer, dtype=torch.float))
            self.softmax = nn.Softmax(0)
        self._cache = {}

    def check_params(self):
        if self.config.shift_target != 0:
            if self.config.subword_pooling not in ('first', 'last'):
                raise ValueError(
                    "Shift target is only supported for first and "
                    "last subword pooling."
                )
        if self.config.subword_pooling not in ('first', 'last') and \
           self.config.layer_pooling == 'weighted_sum':
            raise ValueError(
                "Weighted sum of layers is only supported for first and "
                "last subword pooling."
            )

    def _forward_elementwise_pool(self, embedded, batch):
        subword_pooling = self.config.subword_pooling
        batch_size = embedded.size(0)
        helper = np.arange(batch_size)
        target_idx = np.array(batch.probe_target_idx)
        last = batch.token_starts[helper, target_idx + 1]
        first = batch.token_starts[helper, target_idx]
        target_vecs = []
        for wi in range(batch_size):
            if subword_pooling == 'max':
                o = embedded[wi, first[wi]:last[wi]].max(axis=0).values
            if subword_pooling == 'sum':
                o = embedded[wi, first[wi]:last[wi]].sum(axis=0)
            else:
                o = embedded[wi, first[wi]:last[wi]].mean(axis=0)
            target_vecs.append(o)
        return torch.stack(target_vecs)

    def _forward_last2(self, embedded, batch):
        target_vecs = []
        batch_size = embedded.size(0)
        helper = np.arange(batch_size)
        target_idx = np.array(batch.probe_target_idx)
        last = batch.token_starts[helper, target_idx + 1] - 1
        first = batch.token_starts[helper, target_idx]
        for wi in range(batch_size):
            last1 = embedded[wi, last[wi]]
            if first[wi] == last[wi]:
                last2 = to_cuda(torch.zeros_like(last1))
            else:
                last2 = embedded[wi, last[wi]-1]
            target_vecs.append(torch.cat((last1, last2), 0))
        return torch.stack(target_vecs)

    def _forward_first_plus_last(self, embedded, batch):
        batch_size = embedded.size(0)
        helper = np.arange(batch_size)
        w = self.subword_w
        target_idx = np.array(batch.probe_target_idx)
        last_idx = batch.token_starts[helper, target_idx + 1] - 1
        first_idx = batch.token_starts[helper, target_idx]
        first = embedded[helper, first_idx]
        last = embedded[helper, last_idx]
        target_vecs = w * first + (1 - w) * last
        return target_vecs

    def _forward_lstm(self, embedded, batch):
        batch_size = embedded.size(0)
        helper = np.arange(batch_size)
        target_vecs = []

        target_idx = np.array(batch.probe_target_idx)
        last_idx = batch.token_starts[helper, target_idx + 1]
        first_idx = batch.token_starts[helper, target_idx]

        for wi in range(batch_size):
            lstm_in = embedded[wi, first_idx[wi]:last_idx[wi]].unsqueeze(0)
            _, (h, c) = self.pool_lstm(lstm_in)
            h = torch.cat((h[0], h[1]), dim=-1)
            target_vecs.append(h[0])
        return torch.stack(target_vecs)

    def _forward_mlp(self, embedded, batch):
        batch_size = embedded.size(0)
        helper = np.arange(batch_size)

        target_idx = np.array(batch.probe_target_idx)
        last_idx = batch.token_starts[helper, target_idx + 1]
        first_idx = batch.token_starts[helper, target_idx]

        target_vecs = []
        for wi in range(batch_size):
            mlp_in = embedded[wi, first_idx[wi]:last_idx[wi]]
            weights = self.subword_mlp(mlp_in)
            sweights = self.softmax(weights).transpose(0, 1)
            target = sweights.mm(mlp_in).squeeze(0)
            target_vecs.append(target)
        return torch.stack(target_vecs)

    def forward(self, batch):
        subword_pooling = self.config.subword_pooling
        if subword_pooling in ('first', 'last'):
            target_vecs = self.pooling_func[subword_pooling](batch)
        else:
            # caching not supported
            X = torch.LongTensor(batch.input)
            X = to_cuda(X)
            embedded = self.embedder.embed(X, batch.input_len)
            if self.layer_pooling == 'sum':
                embedded = embedded.sum(0)
            else:
                embedded = embedded[self.layer_pooling]
            target_vecs = self.pooling_func[subword_pooling](embedded, batch)
        mlp_out = self.mlp(target_vecs)
        return mlp_out

    def _forward_first_last(self, batch):
        cache_target_idx = np.array(batch.probe_target_idx)
        cache_key = (
            tuple(np.array(batch.input).flat),
            tuple(cache_target_idx.flat))
        if cache_key not in self._cache:
            X = torch.LongTensor(batch.input)
            X = to_cuda(X)
            embedded = self.embedder.embed(X, batch.input_len)
            batch_size = embedded.size(1)
            helper = np.arange(batch_size)
            target_idx = np.array(batch.probe_target_idx)
            subword_pooling = self.config.subword_pooling
            if subword_pooling == 'first':
                idx = batch.token_starts[helper, target_idx]
            elif subword_pooling == 'last':
                idx = batch.token_starts[helper, target_idx + 1] - 1
            else:
                raise ValueError(f"Subword pooling {subword_pooling} "
                                 "with caching is not supported.")
            if self.config.shift_target:
                shift_max = np.array(batch.input_len) - 1
                idx += self.config.shift_target
                idx = np.minimum(idx, shift_max)
                idx = np.clip(idx, 0, shift_max.max())

            target_vecs = embedded[:, helper, idx]
            if self.layer_pooling == 'weighted_sum':
                self._cache[cache_key] = target_vecs
            elif self.layer_pooling == 'sum':
                self._cache[cache_key] = target_vecs.sum(0)
            else:
                self._cache[cache_key] = target_vecs[self.layer_pooling]
        target_vecs = self._cache[cache_key]

        if self.layer_pooling == 'weighted_sum':
            w = self.softmax(self.weights)
            target_vecs = (w[:, None, None] * target_vecs).sum(0)
        return target_vecs

    def compute_loss(self, target, output):
        target = to_cuda(torch.LongTensor(target.label)).view(-1)
        loss = self.criterion(output, target)
        return loss


class TransformerForSequenceTagging(BaseModel):
    def __init__(self, config, dataset):
        super().__init__(config)
        self.dataset = dataset
        randweights = self.config.randomize_embedding_weights
        self.embedder = Embedder(
            self.config.model_name,
            layer_pooling=self.config.layer_pooling,
            randomize_embedding_weights=randweights)
        self.output_size = len(dataset.vocabs.labels)
        self.dropout = nn.Dropout(self.config.dropout)
        mlp_input_size = self.embedder.hidden_size
        if self.config.subword_pooling == 'lstm':
            sw_lstm_size = self.config.subword_lstm_size
            mlp_input_size = sw_lstm_size
            self.subword_lstm = nn.LSTM(
                self.embedder.hidden_size,
                sw_lstm_size // 2,
                num_layers=1,
                batch_first=True,
                bidirectional=True,
            )
        elif self.config.subword_pooling == 'attn':
            self.subword_mlp = MLP(
                input_size=self.embedder.hidden_size,
                layers=[self.config.subword_mlp_size],
                nonlinearity='ReLU',
                output_size=1
            )
            self.softmax = nn.Softmax(dim=0)
        elif self.config.subword_pooling == 'last2':
            mlp_input_size *= 2
        self.mlp = MLP(
            input_size=mlp_input_size,
            layers=self.config.mlp_layers,
            nonlinearity=self.config.mlp_nonlinearity,
            output_size=self.output_size,
        )
        if self.config.subword_pooling == 'f+l':
            self.subword_w = nn.Parameter(torch.ones(1, dtype=torch.float) / 2)
        self._cache = {}
        self.criterion = nn.CrossEntropyLoss()
        self.pooling_func = {
            'first': self._forward_with_cache,
            'last': self._forward_with_cache,
            'max': self._forward_with_cache,
            'sum': self._forward_with_cache,
            'avg': self._forward_with_cache,
            'last2': self._forward_last2,
            'f+l': self._forward_first_plus_last,
            'lstm': self._forward_lstm,
            'attn': self._forward_mlp,
        }

    def forward(self, batch):
        subword_pooling = self.config.subword_pooling
        out = self.pooling_func[subword_pooling](batch)
        out = self.dropout(out)
        pred = self.mlp(out)
        return pred

    def _forward_lstm(self, batch):
        X = torch.LongTensor(batch.input)
        X = to_cuda(X)
        embedded = self.embedder.embed(X, batch.sentence_subword_len)
        batch_size, seqlen, hidden_size = embedded.size()
        token_lens = batch.token_starts[:, 1:] - batch.token_starts[:, :-1]
        token_maxlen = token_lens.max()
        pad = to_cuda(torch.zeros((1, hidden_size)))
        all_token_vectors = []
        all_token_lens = []
        for bi in range(batch_size):
            for ti in range(batch.sentence_len[bi]):
                first = batch.token_starts[bi][ti+1]
                last = batch.token_starts[bi][ti+2]
                tok_vecs = embedded[bi, first:last]
                this_size = tok_vecs.size(0)
                if this_size < token_maxlen:
                    this_pad = pad.repeat((token_maxlen - this_size, 1))
                    tok_vecs = torch.cat((tok_vecs, this_pad))
                all_token_vectors.append(tok_vecs)
                all_token_lens.append(this_size)
                # _, (h, c) = self.subword_lstm(tok_vecs)
                # h = torch.cat((h[0], h[1]), dim=-1)
                # out.append(h[0])
        lstm_in = torch.stack(all_token_vectors)
        seq = torch.nn.utils.rnn.pack_padded_sequence(
            lstm_in, all_token_lens, enforce_sorted=False, batch_first=True)
        _, (h, c) = self.subword_lstm(seq)
        h = torch.cat((h[0], h[1]), dim=-1)
        return h

    def _forward_mlp(self, batch):
        X = torch.LongTensor(batch.input)
        X = to_cuda(X)
        embedded = self.embedder.embed(X, batch.sentence_subword_len)
        batch_size, seqlen, hidden = embedded.size()
        mlp_weights = self.subword_mlp(embedded).view(batch_size, seqlen)
        outputs = []
        for bi in range(batch_size):
            for ti in range(batch.sentence_len[bi]):
                first = batch.token_starts[bi][ti+1]
                last = batch.token_starts[bi][ti+2]
                if last - 1 == first:
                    outputs.append(embedded[bi, first])
                else:
                    weights = mlp_weights[bi][first:last]
                    weights = self.softmax(weights).unsqueeze(1)
                    v = weights * embedded[bi, first:last]
                    v = v.sum(axis=0)
                    outputs.append(v)
        return torch.stack(outputs)

    def _forward_first_plus_last(self, batch):
        X = torch.LongTensor(batch.input)
        X = to_cuda(X)
        embedded = self.embedder.embed(X, batch.sentence_subword_len)
        batch_size, seqlen, hidden = embedded.size()
        w = self.subword_w
        outputs = []
        for bi in range(batch_size):
            for ti in range(batch.sentence_len[bi]):
                first = batch.token_starts[bi][ti+1]
                last = batch.token_starts[bi][ti+2] - 1
                f = embedded[bi, first]
                la = embedded[bi, last]
                outputs.append(w * f + (1-w) * la)
        return torch.stack(outputs)

    def _forward_last2(self, batch):
        X = torch.LongTensor(batch.input)
        X = to_cuda(X)
        embedded = self.embedder.embed(X, batch.sentence_subword_len)
        batch_size, seqlen, hidden_size = embedded.size()
        outputs = []
        pad = to_cuda(torch.zeros(hidden_size))
        for bi in range(batch_size):
            for ti in range(batch.sentence_len[bi]):
                first = batch.token_starts[bi][ti+1]
                last = batch.token_starts[bi][ti+2] - 1
                if first == last:
                    vec = torch.cat((embedded[bi, last], pad), 0)
                else:
                    vec = torch.cat(
                        (embedded[bi, last], embedded[bi, last-1]), 0)
                outputs.append(vec)
        return torch.stack(outputs)

    def _forward_with_cache(self, batch):
        subword_pooling = self.config.subword_pooling
        X = torch.LongTensor(batch.input)
        cache_key = tuple(np.array(batch.input).flat)
        if cache_key not in self._cache:
            batch_size = X.size(0)
            batch_ids = []
            token_ids = []
            X = to_cuda(X)
            embedded = self.embedder.embed(X, batch.sentence_subword_len)
            if subword_pooling in ('first', 'last'):
                for bi in range(batch_size):
                    sentence_len = batch.sentence_len[bi]
                    batch_ids.append(np.repeat(bi, sentence_len))
                    if subword_pooling == 'first':
                        token_ids.append(batch.token_starts[bi][1:sentence_len + 1])
                    elif subword_pooling == 'last':
                        token_ids.append(
                            np.array(batch.token_starts[bi][2:sentence_len + 2]) - 1)
                batch_ids = np.concatenate(batch_ids)
                token_ids = np.concatenate(token_ids)
                out = embedded[batch_ids, token_ids]
            elif subword_pooling in ('max', 'avg', 'sum'):
                outs = []
                for bi in range(batch_size):
                    for ti in range(batch.sentence_len[bi]):
                        first = batch.token_starts[bi][ti+1]
                        last = batch.token_starts[bi][ti+2]
                        if subword_pooling == 'sum':
                            vec = embedded[bi, first:last].sum(axis=0)
                        elif subword_pooling == 'avg':
                            vec = embedded[bi, first:last].mean(axis=0)
                        elif subword_pooling == 'max':
                            vec = embedded[bi, first:last].max(axis=0).values
                        outs.append(vec)
                out = torch.stack(outs)
            self._cache[cache_key] = out
        return self._cache[cache_key]

    def compute_loss(self, target, output):
        target = to_cuda(torch.LongTensor(target.labels)).view(-1)
        loss = self.criterion(output, target)
        return loss


class Word2vecEmbeddingClassifier(BaseModel):

    def __init__(self, config, dataset):
        super().__init__(config)
        self.dataset = dataset
        self.output_size = len(dataset.vocabs.label)
        self.dropout = nn.Dropout(self.config.dropout)
        self.mlp = MLP(
            input_size=self.dataset.embedding_size,
            layers=self.config.mlp_layers,
            nonlinearity=self.config.mlp_nonlinearity,
            output_size=self.output_size,
        )
        self.criterion = nn.CrossEntropyLoss()

    def forward(self, batch):
        mlp_in = to_cuda(torch.FloatTensor(batch.input))
        mlp_in = self.dropout(mlp_in)
        return self.mlp(mlp_in)

    def compute_loss(self, target, output):
        target = to_cuda(torch.LongTensor(target.label)).view(-1)
        loss = self.criterion(output, target)
        return loss
