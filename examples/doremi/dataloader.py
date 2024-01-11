import dataclasses
import warnings
from typing import Dict, Generator, Iterator, List, Optional, Union

import numpy as np
import torch
from nanotron import logging
from nanotron.config import Config
from nanotron.core import distributed as dist
from nanotron.core.parallel.pipeline_parallelism.tensor_pointer import TensorPointer
from nanotron.core.process_groups import DistributedProcessGroups
from nanotron.core.random import set_random_seed
from nanotron.core.utils import (
    assert_fail_except_rank_with,
    assert_tensor_synced_across_pg,
)
from torch.utils.data import BatchSampler, DataLoader
from torch.utils.data.distributed import DistributedSampler

try:
    import datasets
    from datasets import Dataset, DatasetDict, Features, Sequence, Value, concatenate_datasets, load_dataset
    from transformers import (
        PreTrainedTokenizerBase,
    )
    from transformers.trainer_pt_utils import DistributedSamplerWithLoop
except ImportError:
    warnings.warn("Datasets and/or Transformers not installed, you'll be unable to use the dataloader.")


logger = logging.get_logger(__name__)


def sanity_check_dataloader(
    dataloader: Iterator[Dict[str, Union[torch.Tensor, TensorPointer]]], dpg: DistributedProcessGroups, config: Config
) -> Iterator[Dict[str, Union[torch.Tensor, TensorPointer]]]:
    for batch in dataloader:
        micro_batch = {
            k: v if isinstance(v, TensorPointer) else v.to("cuda", memory_format=torch.contiguous_format)
            for k, v in batch.items()
        }

        if not config.general.ignore_sanity_checks:
            # SANITY CHECK: Check input are not the same across DP
            for key, value in sorted(micro_batch.items(), key=lambda x: x[0]):
                if isinstance(value, TensorPointer):
                    continue

                if "mask" in key:
                    # It's fine if mask is the same across DP
                    continue

                with assert_fail_except_rank_with(AssertionError, rank_exception=0, pg=dpg.dp_pg):
                    assert_tensor_synced_across_pg(tensor=value, pg=dpg.dp_pg, msg=lambda err: f"{key} {err}")

            # SANITY CHECK: Check input are synchronized throughout TP
            for key, value in sorted(micro_batch.items(), key=lambda x: x[0]):
                if isinstance(value, TensorPointer):
                    continue
                assert_tensor_synced_across_pg(
                    tensor=value,
                    pg=dpg.tp_pg,
                    msg=lambda err: f"{key} are not synchronized throughout TP {err}",
                )

            # SANITY CHECK: Check that input are synchronized throughout PP
            # TODO @thomasw21: That's really hard to test as input gets sharded across the PP, let's assume it works for now.

            # SANITY CHECK: Check that an input only exists on the PP rank responsible for it
            # TODO @nouamanetazi: add this test
        yield micro_batch


# Adapted from h4/src/h4/data/loading.py
def get_datasets(
    hf_dataset_or_datasets: Union[dict, str],
    splits: Optional[Union[List[str], str]] = ["train", "test"],
) -> DatasetDict:
    """
    Function to load dataset directly from DataArguments.

    Args:
        hf_dataset_or_datasets (Union[dict, str]): dict or string. When all probabilities are 1, we concatenate the datasets instead of sampling from them.
        splits (Optional[List[str]], optional): Section of the dataset to load, defaults to "train", "test"
            Can be one of `train_ift`, `test_rl`, or `..._rm` etc. H4 datasets are divided into 6 subsets for training / testing.

    Returns
        DatasetDict: DatasetDict object containing the dataset of the appropriate section with test + train parts.
    """

    if isinstance(splits, str):
        splits = [splits]

    if isinstance(hf_dataset_or_datasets, dict):
        # Structure of the config to read the datasets and their mix
        # datasets_mixer:
        #     - 'dataset1': 0.5
        #     - 'dataset2': 0.3
        #     - 'dataset3': 0.2
        raw_datasets = _get_dataset_mix(hf_dataset_or_datasets, splits=splits)
    elif isinstance(hf_dataset_or_datasets, str):
        # e.g. Dataset = "HuggingFaceH4/testing_alpaca_small"
        # Note this returns things other than just train/test, which may not be intended
        raw_datasets = DatasetDict()
        for split in splits:
            raw_datasets[split] = load_dataset(
                hf_dataset_or_datasets,
                split=split,
            )
    else:
        raise ValueError(f"hf_dataset_or_datasets must be a dict or string but is {type(hf_dataset_or_datasets)}")

    return raw_datasets


# Adapted from h4/src/h4/data/loading.py
def _get_dataset_mix(dataset_dict: dict, splits: List[str] = None, seed=42) -> DatasetDict:
    """
    Helper function to load dataset mix from dict configuration.

    Args:
        dataset_dict (dict): Dictionary containing the dataset names and their training proportions. By default, all test proportions are 1.
        splits (Optional[List[str]], optional): Section of the dataset to load, defaults to "train", "test"
            Can be one of `train_{ift,rm,rl}` and `test_{ift,rm,rl}`. Our datasets are typically divided into 6 subsets for training / testing.
    """
    raw_datasets = DatasetDict()
    raw_train_datasets = []
    raw_test_datasets = []
    fracs = []
    for ds, frac in dataset_dict.items():
        if frac < 0:
            raise ValueError(f"Dataset fraction for dataset {ds} is negative. (= {frac})")

        fracs.append(frac)
        for split in splits:
            if "train" in split:
                raw_train_datasets.append(
                    load_dataset(
                        ds,
                        split=split,
                    )
                )
            elif "test" in split:
                raw_test_datasets.append(
                    load_dataset(
                        ds,
                        split=split,
                    )
                )
            else:
                raise ValueError(f"Split type {split} not recognized as one of test or train.")

    if len(raw_train_datasets) > 0:
        train_subsets = []
        for dataset, frac in zip(raw_train_datasets, fracs):
            train_subset = dataset.select(range(int(frac * len(dataset))))
            train_subsets.append(train_subset)
        raw_datasets["train"] = concatenate_datasets(train_subsets).shuffle(seed=seed)

    # No subsampling for test datasets to enable fair comparison across models
    if len(raw_test_datasets) > 0:
        raw_datasets["test"] = concatenate_datasets(raw_test_datasets).shuffle(seed=seed)

    if len(raw_datasets) == 0:
        raise ValueError(
            f"Dataset {dataset_dict} not recognized with split {split}. Check the dataset has been correctly formatted."
        )

    return raw_datasets


def dummy_infinite_data_generator(
    micro_batch_size: int,
    sequence_length: int,
    input_pp_rank: int,
    output_pp_rank: int,
    vocab_size: int,
    seed: int,
    dpg: DistributedProcessGroups,
):
    def dummy_infinite_data_generator() -> Generator[Dict[str, Union[torch.Tensor, TensorPointer]], None, None]:
        # Random generator
        generator = torch.Generator(device="cuda")
        # Make sure that TP are synced always
        generator.manual_seed(seed * (1 + dist.get_rank(dpg.dp_pg)) * (1 + dist.get_rank(dpg.pp_pg)))

        while True:
            yield {
                "input_ids": torch.randint(
                    0,
                    vocab_size,
                    (micro_batch_size, sequence_length),
                    dtype=torch.long,
                    device="cuda",
                    generator=generator,
                )
                if dist.get_rank(dpg.pp_pg) == input_pp_rank
                else TensorPointer(group_rank=input_pp_rank),
                "input_mask": torch.ones(
                    micro_batch_size,
                    sequence_length,
                    dtype=torch.bool,
                    device="cuda",
                )
                if dist.get_rank(dpg.pp_pg) == input_pp_rank
                else TensorPointer(group_rank=input_pp_rank),
                "label_ids": torch.randint(
                    0,
                    vocab_size,
                    (micro_batch_size, sequence_length),
                    dtype=torch.long,
                    device="cuda",
                    generator=generator,
                )
                if dist.get_rank(dpg.pp_pg) == output_pp_rank
                else TensorPointer(group_rank=output_pp_rank),
                "label_mask": torch.ones(
                    micro_batch_size,
                    sequence_length,
                    dtype=torch.bool,
                    device="cuda",
                )
                if dist.get_rank(dpg.pp_pg) == output_pp_rank
                else TensorPointer(group_rank=output_pp_rank),
            }

    return dummy_infinite_data_generator


# Adapted from https://github.com/huggingface/accelerate/blob/a73898027a211c3f6dc4460351b0ec246aa824aa/src/accelerate/data_loader.py#L781C1-L824C28
class SkipBatchSampler(BatchSampler):
    """
    A `torch.utils.data.BatchSampler` that skips the first `n` batches of another `torch.utils.data.BatchSampler`.
    Note that in case of DDP, we skip batches on each rank, so a total of `skip_batches * dpg.dp_pg.size()` batches
    """

    def __init__(self, batch_sampler: BatchSampler, skip_batches: int, dp_size: int):
        self.batch_sampler = batch_sampler
        # In case of DDP, we skip batches on each rank, so a total of `skip_batches * dpg.dp_pg.size()` batches
        self.skip_batches = skip_batches // dp_size

    def __iter__(self):
        for index, samples in enumerate(self.batch_sampler):
            if index >= self.skip_batches:
                yield samples

    @property
    def total_length(self):
        return len(self.batch_sampler)

    def __len__(self):
        return len(self.batch_sampler) - self.skip_batches


def set_tensor_pointers(
    input_dict: Dict[str, Union[torch.Tensor, TensorPointer]], group: dist.ProcessGroup, group_rank: int
) -> Dict[str, Union[torch.Tensor, TensorPointer]]:
    """Make sure only the group_rank rank has the data, others have TensorPointers."""
    return {
        k: v if dist.get_rank(group) == group_rank else TensorPointer(group_rank=group_rank)
        for k, v in input_dict.items()
    }


### CAUSAL LANGUAGE MODELING ###
def clm_process(
    raw_dataset: Dataset,
    tokenizer: PreTrainedTokenizerBase,
    text_column_name: str,
    dataset_processing_num_proc_per_process: int,
    dataset_overwrite_cache: bool,
    sequence_length: int,
):
    """Concatenate all texts from raw_dataset and generate chunks of `sequence_length + 1`, where chunks overlap by a single token."""
    # Adapted from https://github.com/huggingface/transformers/blob/47e1676255e5dd86b9541f734cd4f4bdcbb50f4a/examples/pytorch/language-modeling/run_clm.py#L391-L439

    def group_texts(examples: Dict[str, List[np.ndarray]]) -> Dict[str, List[np.ndarray]]:
        # Concatenate all texts.
        concatenated_examples = {k: np.concatenate(v) for k, v in examples.items()}
        total_length = len(concatenated_examples[next(iter(examples.keys()))])
        # WARNING: We drop the small remainder, we could add padding if the model supported it instead of this drop, you can
        # customize this part to your needs.
        if total_length >= sequence_length + 1:
            total_length = ((total_length - 1) // sequence_length) * sequence_length + 1
        # Split by chunks of sequence_length.
        result = {
            k: [
                t[i : i + sequence_length + 1] for i in range(0, total_length - (sequence_length + 1), sequence_length)
            ]
            for k, t in concatenated_examples.items()
        }
        return result

    def _tokenize_and_group_texts(texts: List[str]) -> Dict[str, List[np.ndarray]]:
        tokenized_batch = tokenizer.batch_encode_plus(texts, return_attention_mask=False, return_token_type_ids=False)
        tokenized_batch = {k: [np.array(tokenized_texts) for tokenized_texts in v] for k, v in tokenized_batch.items()}
        return group_texts(tokenized_batch)

    train_dataset = raw_dataset.map(
        _tokenize_and_group_texts,
        input_columns=text_column_name,
        remove_columns=raw_dataset.column_names,
        features=Features({"input_ids": Sequence(feature=Value(dtype="int64"), length=sequence_length + 1)}),
        batched=True,
        num_proc=dataset_processing_num_proc_per_process,
        load_from_cache_file=not dataset_overwrite_cache,
        desc=f"Grouping texts in chunks of {sequence_length+1}",
    )
    return train_dataset


# Adapted from: https://github.com/huggingface/transformers/blob/47e1676255e5dd86b9541f734cd4f4bdcbb50f4a/src/transformers/data/data_collator.py#L607
@dataclasses.dataclass
class DataCollatorForCLM:
    """
    Data collator used for causal language modeling.

    GPT2Tokenizer doesn't have a _pad_token. For tokenizers that do, inputs can be dynamically padded to the maximum length of a batch if they
    are not all of the same length. see: https://github.com/huggingface/transformers/blob/47e1676255e5dd86b9541f734cd4f4bdcbb50f4a/src/transformers/data/data_collator.py#L394-L430
    """

    sequence_length: int
    input_pp_rank: int
    output_pp_rank: int
    dpg: DistributedProcessGroups

    def __call__(self, examples: List[Dict[str, List[np.ndarray]]]) -> Dict[str, Union[torch.Tensor, TensorPointer]]:
        # Process the case when "input_ids" doesn't exist
        current_pp_rank = dist.get_rank(self.dpg.pp_pg)
        if current_pp_rank not in [
            self.input_pp_rank,
            self.output_pp_rank,
        ]:
            assert all(len(example) == 0 for example in examples)
            return {
                "input_ids": TensorPointer(self.input_pp_rank),
                "input_mask": TensorPointer(self.input_pp_rank),
                "label_ids": TensorPointer(self.output_pp_rank),
                "label_mask": TensorPointer(self.output_pp_rank),
            }

        # Make sure we load only what's necessary, ie we only load a `input_ids` column.
        assert all(list(example.keys()) == ["input_ids"] for example in examples)

        # TODO @nouamanetazi: Is it better to have examples as np.array or torch.Tensor?
        input_ids = np.vstack([examples[i]["input_ids"] for i in range(len(examples))])  # (b, s)
        batch_size, expanded_input_length = input_ids.shape

        result: Dict[str, Union[np.ndarray, TensorPointer]] = {}

        result["input_ids"] = TensorPointer(group_rank=self.input_pp_rank)
        result["input_mask"] = TensorPointer(group_rank=self.input_pp_rank)
        result["label_ids"] = TensorPointer(group_rank=self.output_pp_rank)
        result["label_mask"] = TensorPointer(group_rank=self.output_pp_rank)

        assert (
            expanded_input_length == self.sequence_length + 1
        ), f"Samples should be of length {self.sequence_length + 1} (seq_len+1), but got {expanded_input_length}"

        # Process inputs: last token is the label
        if current_pp_rank == self.input_pp_rank:
            result["input_ids"] = input_ids[:, :-1]
            result["input_mask"] = np.ones((batch_size, self.sequence_length), dtype=np.bool_)

        # Process labels: shift them to the left
        if current_pp_rank == self.output_pp_rank:
            result["label_ids"] = input_ids[:, 1:]
            result["label_mask"] = np.ones((batch_size, self.sequence_length), dtype=np.bool_)

        if isinstance(result["input_ids"], torch.Tensor) and result["input_ids"].shape[-1] != self.sequence_length:
            raise ValueError(
                f"`labels` are incorrectly preprocessed. `labels` length is {result['input_ids'].shape[-1]}, but should be"
                f" {self.sequence_length}."
            )
        if isinstance(result["label_ids"], torch.Tensor) and result["label_ids"].shape[-1] != self.sequence_length:
            raise ValueError(
                f"`labels` are incorrectly preprocessed. `labels` length is {result['label_ids'].shape[-1]}, but should be"
                f" {self.sequence_length}."
            )

        # Cast np.array to torch.Tensor
        result = {k: v if isinstance(v, TensorPointer) else torch.from_numpy(v) for k, v in result.items()}
        return result


# Adapted from https://github.com/huggingface/transformers/blob/47e1676255e5dd86b9541f734cd4f4bdcbb50f4a/src/transformers/trainer.py#L763-L835
def _get_train_sampler(
    domain_weights: torch.Tensor,
    dp_size: int,
    dp_rank: int,
    # TODO(xrsrke): add type hints
    train_datasets: Dataset,
    seed: int,
    use_loop_to_round_batch_size: bool,
    consumed_train_samples: int,
    micro_batch_size: Optional[int] = None,
    drop_last: Optional[bool] = True,
) -> Optional[torch.utils.data.Sampler]:
    """returns sampler that restricts data loading to a subset of the dataset proper to the DP rank"""

    # Build the sampler.
    # TODO @nouamanetazi: Support group_by_length: https://github.com/huggingface/transformers/blob/47e1676255e5dd86b9541f734cd4f4bdcbb50f4a/src/transformers/trainer.py#L783-L810

    if use_loop_to_round_batch_size:
        assert micro_batch_size is not None
        # loops at the end back to the beginning of the shuffled samples to make each process have a round multiple of batch_size samples.
        sampler = DistributedSamplerWithLoop(
            train_datasets,
            batch_size=micro_batch_size,
            num_replicas=dp_size,
            rank=dp_rank,
            seed=seed,
            drop_last=drop_last,
        )
    else:
        # NOTE: this the one that doremi use
        import math

        # class DistributedSamplerForDoReMi(DistributedSampler):
        #     def __init__(self, domain_weights: torch.Tensor, datasets: List[Dataset], batch_size: int, **kwargs):
        #         # Assuming datasets is a list of PyTorch Dataset objects for each domain
        #         # domain_weights is a tensor where each element is the weight of the corresponding domain
        #         super().__init__(datasets, **kwargs)
        #         self.datasets = datasets
        #         self.batch_size = batch_size
        #         self.domain_weights = domain_weights / domain_weights.sum()  # Normalize domain weights
        #         self.total_size = self.calculate_total_size()

        #     def calculate_total_size(self):
        #         # Calculate total size of the sampler considering all domains
        #         total_samples = sum(len(d) for d in self.datasets)
        #         return math.ceil(total_samples / self.batch_size) * self.batch_size

        #     def __iter__(self):
        #         # Generate indices for each domain
        #         domain_indices = []

        #         lengths = [len(d) for d in self.datasets]
        #         offsets = np.cumsum([0] + lengths[:-1])

        #         for i, dataset in enumerate(self.datasets):
        #             # number of samples
        #             num_samples = int(len(dataset) * self.domain_weights[i].item())
        #             # num_samples = int(self.batch_size * self.domain_weights[i].item())
        #             local_indices = np.random.choice(len(dataset), num_samples, replace=False)
        #             # NOTE: align the indicies across the combined dataset
        #             global_indices = local_indices + offsets[i]
        #             # NOTE: add offsets
        #             domain_indices.extend(global_indices)

        #         # Ensure we have the right total size
        #         np.random.shuffle(domain_indices)
        #         domain_indices = domain_indices[: self.total_size]
        #         # yield domain_indices

        #         # Yield indices in batches
        #         for i in range(0, len(domain_indices), self.batch_size):
        #             yield domain_indices[i : i + self.batch_size]

        class DistributedSamplerForDoReMi(DistributedSampler):
            def __init__(self, domain_weights: torch.Tensor, datasets: List[Dataset], batch_size: int, **kwargs):
                # Assuming datasets is a list of PyTorch Dataset objects for each domain
                # domain_weights is a tensor where each element is the weight of the corresponding domain
                super().__init__(datasets, **kwargs)
                self.datasets = datasets
                self.batch_size = batch_size
                self.domain_weights = domain_weights / domain_weights.sum()  # Normalize domain weights
                self.total_size = self.calculate_total_size()

            def calculate_total_size(self):
                # Calculate total size of the sampler considering all domains
                total_samples = sum(len(d) for d in self.datasets)
                return math.ceil(total_samples / self.batch_size) * self.batch_size

            def __iter__(self):
                domain_indices = []

                lengths = [len(d) for d in self.datasets]
                offsets = np.cumsum([0] + lengths[:-1])

                for i, dataset in enumerate(self.datasets):
                    dataset_partition_size = len(dataset) // self.num_replicas
                    dataset_partition_offsets = self.rank * dataset_partition_size
                    num_samples = int(dataset_partition_size * self.domain_weights[i].item())

                    local_indices = (
                        np.random.choice(dataset_partition_size, num_samples, replace=False)
                        + dataset_partition_offsets
                    )
                    # NOTE: align the indicies across the combined dataset
                    global_indices = local_indices + offsets[i]
                    domain_indices.extend(global_indices)

                np.random.shuffle(domain_indices)
                domain_indices = domain_indices[: self.total_size]

                # Yield indices in batches
                for i in range(0, len(domain_indices), self.batch_size):
                    yield domain_indices[i : i + self.batch_size]

        sampler = DistributedSamplerForDoReMi(
            domain_weights,
            train_datasets,
            batch_size=micro_batch_size,
            num_replicas=dp_size,
            rank=dp_rank,
            seed=seed,
            drop_last=drop_last,
        )
        # sampler = DistributedSampler(train_dataset, num_replicas=dp_size, rank=dp_rank, seed=seed, drop_last=drop_last)

    if consumed_train_samples > 0:
        sampler = SkipBatchSampler(sampler, skip_batches=consumed_train_samples, dp_size=dp_size)

    return sampler


# Adapted from https://github.com/huggingface/transformers/blob/47e1676255e5dd86b9541f734cd4f4bdcbb50f4a/src/transformers/trainer.py#L837
def get_train_dataloader(
    domain_weights: torch.Tensor,
    train_datasets: Dataset,
    sequence_length: int,
    dpg: DistributedProcessGroups,
    input_pp_rank: int,
    output_pp_rank: int,
    micro_batch_size: int,
    consumed_train_samples: int,
    dataloader_num_workers: int,
    seed_worker: int,
    dataloader_drop_last: bool = True,
    dataloader_pin_memory: bool = True,
    use_loop_to_round_batch_size: bool = False,
) -> DataLoader:

    # if not isinstance(train_datasets, datasets.Dataset):
    #     raise ValueError(f"training requires a datasets.Dataset, but got {type(train_datasets)}")

    # Only some rank require to run the dataloader.
    if dist.get_rank(dpg.pp_pg) not in [
        input_pp_rank,
        output_pp_rank,
    ]:
        # dataset has to have a single column, with `input_ids` as the column name
        # TODO: use a single name
        train_datasets = train_datasets["domain_0"]
        assert train_datasets.column_names == ["input_ids"]
        dataset_length = len(train_datasets)
        train_datasets = train_datasets.remove_columns(column_names="input_ids")
        assert (
            len(train_datasets) == 0
        ), f"Dataset has to be empty after removing the `input_ids` column. Current dataset: {train_datasets}"
        # HACK as if we remove the last column of a train_dataset, it becomes empty and it's number of rows becomes empty.
        train_datasets = EmptyInfiniteDataset(length=dataset_length)
        # No need to spawn a lot of workers, we can just use main
        dataloader_num_workers = 0
    else:
        # train_dataset = train_dataset.with_format(type="numpy", columns=["input_ids"], output_all_columns=True)
        # TODO(xrsrke): parallelize this
        train_datasets = [
            train_datasets[domain_name].with_format(type="numpy", columns=["input_ids"], output_all_columns=True)
            for domain_name in train_datasets
        ]

    data_collator = DataCollatorForCLM(
        sequence_length=sequence_length,
        input_pp_rank=input_pp_rank,
        output_pp_rank=output_pp_rank,
        dpg=dpg,
    )

    # TODO @nouamanetazi: Remove unused columns: https://github.com/huggingface/transformers/blob/47e1676255e5dd86b9541f734cd4f4bdcbb50f4a/src/transformers/trainer.py#L852
    # TODO @nouamanetazi: Support torch.utils.data.IterableDataset: https://github.com/huggingface/transformers/blob/47e1676255e5dd86b9541f734cd4f4bdcbb50f4a/src/transformers/trainer.py#L855-L872

    train_datasets = [d for d in train_datasets if len(d) > 0]
    train_sampler = _get_train_sampler(
        domain_weights=domain_weights,
        dp_size=dpg.dp_pg.size(),
        dp_rank=dist.get_rank(dpg.dp_pg),
        train_datasets=train_datasets,
        seed=seed_worker,
        use_loop_to_round_batch_size=use_loop_to_round_batch_size,
        micro_batch_size=micro_batch_size,
        drop_last=dataloader_drop_last,
        consumed_train_samples=consumed_train_samples,
    )

    from torch.utils.data import Dataset

    # class CombinedDataset(Dataset):
    #     def __init__(self, datasets: List[Dataset]):
    #         self.datasets = datasets
    #         self.lengths = [len(d) for d in datasets]
    #         self.offsets = np.cumsum([0] + self.lengths[:-1])

    #     def __len__(self):
    #         return sum(self.lengths)

    #     def __getitem__(self, idx):
    #         # for i, offset in enumerate(self.offsets):
    #         #     if idx < offset + self.lengths[i]:
    #         #         return self.datasets[i][idx - offset]
    #         # raise IndexError("Index out of range")
    #         # Map the global index to the corresponding domain and local index
    #         for i, offset in enumerate(self.offsets):
    #             if idx < offset + self.lengths[i]:
    #                 # Calculate the local index within the domain
    #                 local_idx = idx - offset
    #                 # Get the dataset for the corresponding domain
    #                 domain_dataset = self.datasets[i]
    #                 # Use the sampler's indices to retrieve the sample
    #                 return domain_dataset[local_idx]

    class CombinedDataset(Dataset):
        def __init__(self, datasets: List[Dataset]):
            self.datasets = datasets
            # Pdb().set_trace()
            self.lengths = [len(d) for d in datasets]
            self.offsets = np.cumsum([0] + self.lengths[:-1])

        def __len__(self):
            return sum(self.lengths)

        def __getitem__(self, global_indices):
            # Handle a list of global indices
            if isinstance(global_indices, list):
                return [self.get_sample(global_idx) for global_idx in global_indices]
            else:
                # If a single index is provided, process it directly
                return self.get_sample(global_indices)

        def get_sample(self, global_idx):
            # Identify the dataset corresponding to the global_idx and return the sample
            # Pdb().set_trace()
            dataset_idx, local_idx = self.get_dataset_and_local_index(global_idx)
            # if (not isinstance(dataset_idx, int)) or (not isinstance(local_idx, int)):
            #     assert 1 == 1
            # TODO(xrsrke): don't fix the name
            return self.datasets[dataset_idx]["input_ids"][local_idx]

        def get_dataset_and_local_index(self, global_idx):
            # Find the dataset index and local index for the given global index
            for i, offset in enumerate(self.offsets):
                if global_idx < offset + self.lengths[i]:
                    return i, global_idx - offset

            raise IndexError(f"Index out of range, global_idx={global_idx}")

    comebined_dataset = CombinedDataset(train_datasets)

    # class DataLoaderForDoReMi(DataLoader):
    #     def __init__(self, dataset, **kwargs):
    #         super().__init__(dataset, **kwargs)
    #         self.dataset = dataset

    #     def __iter__(self):
    #         # take t

    return DataLoader(
        comebined_dataset,
        batch_size=micro_batch_size,
        sampler=train_sampler,
        collate_fn=data_collator,
        drop_last=dataloader_drop_last,  # we also drop_last in `clm_process()`
        num_workers=dataloader_num_workers,
        pin_memory=dataloader_pin_memory,
        worker_init_fn=get_dataloader_worker_init(dp_rank=dist.get_rank(dpg.dp_pg)),
        # TODO @thomasw21: I'm not sure but this doesn't seem to work at all.
        # pin_memory_device="cuda",
    )


def get_dataloader_worker_init(dp_rank: int):
    """Creates random states for each worker in order to get different state in each workers"""

    def dataloader_worker_init(worker_id):
        # Dataloader is TP/PP synced in random states
        seed = 2 ** (1 + worker_id) * 3 ** (1 + dp_rank) % (2**32)
        set_random_seed(seed)

    return dataloader_worker_init


class EmptyInfiniteDataset:
    """Hack as removing all columns from a datasets.Dataset makes the number of rows 0."""

    def __init__(self, length: int):
        self._length = length

    def __getitem__(self, item) -> Dict:
        if isinstance(item, int):
            return {}
        raise NotImplementedError(f"{item} of type {type(item)} is not supported yet")

    def __len__(self) -> int:
        return self._length
