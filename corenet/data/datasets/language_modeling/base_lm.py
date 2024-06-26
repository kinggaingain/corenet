#
# For licensing see accompanying LICENSE file.
# Copyright (C) 2024 Apple Inc. All Rights Reserved.
#

import argparse
import random
import re
import string
from typing import Dict, Iterator, Optional

import torch
from torch import Tensor

from corenet.data.datasets.dataset_base import BaseIterableDataset
from corenet.data.text_tokenizer import build_tokenizer


def _process_text(text: str) -> str:
    """Process text to identify low-length content.

    This processing step follows SlimPajama.

    Citation:
        @misc{cerebras2023slimpajama,
            author = {Soboleva, Daria and Al-Khateeb, Faisal and Myers, Robert and Steeves, Jacob R and Hestness, Joel and Dey, Nolan},
            title = {{SlimPajama: A 627B token cleaned and deduplicated version of RedPajama}},
            month = June,
            year = 2023,
            url = {https://huggingface.co/datasets/cerebras/SlimPajama-627B},
            howpublished = {https://www.cerebras.net/blog/slimpajama-a-627b-token-cleaned-and-deduplicated-version-of-redpajama},
        }

    Args:
        text: Input text sequence.

    Returns:
        Processed text sequence.
    """
    text = text.lower()
    text = text.translate(str.maketrans("", "", string.punctuation))
    text = re.sub(r"\s+", " ", text.strip())
    return text


class BaseLMIterableDataset(BaseIterableDataset):
    """Base class for language modeling datasets.

    Args:
        opts: Command-line arguments.
    """

    def __init__(self, opts: argparse.Namespace, *args, **kwargs) -> None:
        super().__init__(opts, *args, **kwargs)
        self.shuffle_data = getattr(opts, "dataset.language_modeling.shuffle_data")
        self._rng = random.Random(self.seed)

        self.sequence_length = getattr(
            opts, "dataset.language_modeling.sequence_length"
        )
        self.min_tokens_per_text = getattr(
            opts, "dataset.language_modeling.min_tokens_per_text"
        )
        self.min_characters_per_text = getattr(
            opts, "dataset.language_modeling.min_characters_per_text"
        )

        self.tokenizer = build_tokenizer(opts)

    @property
    def pad_token_id(self) -> int:
        """Index corresponding to padding token."""
        return self.tokenizer.pad_token_id

    @property
    def vocab_size(self) -> int:
        """Vocabulary size."""
        return self.tokenizer.vocab_size

    @property
    def seed(self) -> int:
        """Seed for initializing random state."""
        opts = self.opts
        return getattr(opts, "dataset.language_modeling.random_seed")

    def _tokenize_text(self, text: str) -> Optional[Dict[str, Tensor]]:
        """Convert input text into tokens.

        Args:
            text: Input text sequence.

        Returns:
            For valid sequences, dictionary containing 1D tensors with token indices for
            input samples and target labels. The shape of tensors is [sequence length]. Otherwise,
            None is returned.

        ...note:
            To study the effect of multiple tokenizations, we do 'on-the-fly' tokenization.
            Pre-training text corpora are often noisy and may contain low-length sequences.
            To deal such text sequences, we apply two filtering methods:
                1. We process the text and check if the number of characters in the text sequence
                    are less than the specified threshold or not. If it is, then we skip such sequences.
                2. After tokenizing the sequence, we check for the number of tokens. If they are smaller
                    than the pre-defined threshold, then such sequences are skipped.
        """
        if len(_process_text(text)) < self.min_characters_per_text:
            return None

        tokenized_text = self.tokenizer(text)
        n_tokens = tokenized_text.shape[0]

        if n_tokens < self.min_tokens_per_text:
            return None

        # In language modeling, the target sequence is generated by shifting the input sequence by one position.
        valid_seq_length = min(n_tokens, self.sequence_length + 1)
        content_tensor = torch.full(
            size=(self.sequence_length + 1,),
            fill_value=self.pad_token_id,
            dtype=torch.long,
        )

        content_tensor[:valid_seq_length] = tokenized_text[:valid_seq_length]
        return {
            "samples": content_tensor[:-1],
            "targets": content_tensor[1:],
        }

    @classmethod
    def add_arguments(cls, parser: argparse.ArgumentParser) -> argparse.ArgumentParser:
        if cls == BaseLMIterableDataset:
            group = parser.add_argument_group(cls.__name__)
            group.add_argument(
                "--dataset.language-modeling.sequence-length",
                type=int,
                default=2048,
                help="Tokenized sequence length. Defaults to 2048.",
            )
            group.add_argument(
                "--dataset.language-modeling.min-tokens-per-text",
                type=int,
                default=0,
                help="Minimum number of tokens per text after tokenization. "
                "This flag allows us to skip short text sequences and avoid excessive padding. Defaults to 0.",
            )
            group.add_argument(
                "--dataset.language-modeling.min-characters-per-text",
                type=int,
                default=0,
                help="Minimum number of characters in a text sequence before tokenization. "
                "This flag allows us to skip short text sequences. Defaults to 0.",
            )
            group.add_argument(
                "--dataset.language-modeling.shuffle-data",
                action="store_true",
                default=False,
                help="The pre-training corpora consist of multiple text files. "
                "This flag can be utilized to enable shuffling of these data files. It defaults to False, "
                "with the note that the user is responsible for implementing the shuffling operation.",
            )
            group.add_argument(
                "--dataset.language-modeling.random-seed",
                type=int,
                default=0,
                help="Random seed for shuffling data files. Defaults to 0.",
            )
        return parser

    def generate_sample(
        self, scaled_rank: int, scaled_world_size: int
    ) -> Iterator[Dict[str, Tensor]]:
        """Generate input and labels.

        Args:
            scaled_rank: Scaled rank. It represents the unique identifier assigned to each process within a
                distributed system. The total number of processes is determined by multiplying the number
                of available GPUs by the number of dataset workers. This scaling ensures that each process
                has a distinct and consistent identification, preventing duplicated data sampling and
                facilitating efficient coordination across the distributed environment.
            scaled_world_size: Scaled world size. It represents the combined count of processes involved in
                distributed system. It is determined by multiplying the number of available GPUs (a.k.a., world size)
                by the number of dataset workers.

        Yields:
            This function should yield a dictionary containing 'samples' and 'targets' as keys corresponding to
            the input and label of a sample, respectively. The shape of input and label tensors is [sequence length].

        ...note:
            Iterable datasets can generate duplicate content across different multi-processing workers. To avoid this,
            the rank and world size are scaled, so each worker can process a different content file.

            Child classes must implement 'generate_sample' function correctly.
        """
        raise NotImplementedError(
            "Child class must implement 'generate_sample' function."
        )

    def __iter__(self) -> Iterator[Dict[str, Tensor]]:
        """Returns an iterator over the dataset.

        Yields:
            A dictionary containing 'samples' and 'targets' as keys corresponding to
            the input and label of a sample, respectively. The shape of input and label tensors is [sequence length].
        """

        # scale the rank and world size to deal with multiprocessing and distributed training.
        scaled_world_size = self.world_size * self.num_workers
        scaled_rank = self.rank * self.num_workers + self.worker_id
        yield from self.generate_sample(
            scaled_rank=scaled_rank, scaled_world_size=scaled_world_size
        )

    def extra_repr(self) -> str:
        return (
            f"\n\tvocab_size={self.vocab_size}"
            f"\n\tpad_token_id={self.pad_token_id}"
            f"\n\tmin_characters_per_text={self.min_characters_per_text}"
            f"\n\tmin_tokens_per_text={self.min_tokens_per_text}"
            f"\n\tshuffle={self.shuffle_data}"
        )
