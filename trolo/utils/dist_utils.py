import os
import time
import random
import numpy as np
import atexit

import torch
import torch.nn as nn
import torch.distributed
import torch.backends.cudnn
import logging

from torch.nn.parallel import DataParallel as DP
from torch.nn.parallel import DistributedDataParallel as DDP
from torch.distributed.fsdp import FullyShardedDataParallel as FSDP

from torch.utils.data import DistributedSampler
# from torch.utils.data.dataloader import DataLoader
from ..data import DataLoader

LOGGER = logging.getLogger(__name__)

def setup_distributed(
    print_rank: int = 0,
    print_method: str = "builtin",
    seed: int = None,
):
    """
    env setup
    args:
        print_rank,
        print_method, (builtin, rich)
        seed,
    """
    try:
        # https://pytorch.org/docs/stable/elastic/run.html
        RANK = int(os.getenv("RANK", -1))
        LOCAL_RANK = int(os.getenv("LOCAL_RANK", -1))
        WORLD_SIZE = int(os.getenv("WORLD_SIZE", 1))

        # torch.distributed.init_process_group(backend=backend, init_method='env://')
        torch.distributed.init_process_group(init_method="env://")
        torch.distributed.barrier()

        rank = torch.distributed.get_rank()
        torch.cuda.set_device(rank)
        torch.cuda.empty_cache()
        enabled_dist = True
        if get_rank() == print_rank:
            LOGGER.info("Initialized distributed mode...")

    except Exception:
        enabled_dist = False
        LOGGER.warning("Not init distributed mode.")

    setup_print(get_rank() == print_rank, method=print_method)
    if seed is not None:
        setup_seed(seed)

    return enabled_dist


def setup_print(is_main, method="builtin"):
    """This function disables printing when not in master process"""
    import builtins as __builtin__

    if method == "builtin":
        builtin_print = __builtin__.print

    elif method == "rich":
        import rich

        builtin_print = rich.print

    else:
        raise AttributeError("")

    def print(*args, **kwargs):
        force = kwargs.pop("force", False)
        if is_main or force:
            builtin_print(*args, **kwargs)

    __builtin__.print = print


def is_dist_available_and_initialized():
    if not torch.distributed.is_available():
        return False
    if not torch.distributed.is_initialized():
        return False
    return True


@atexit.register
def cleanup():
    """cleanup distributed environment"""
    if is_dist_available_and_initialized():
        torch.distributed.barrier()
        torch.distributed.destroy_process_group()


def get_rank():
    if not is_dist_available_and_initialized():
        return 0
    return torch.distributed.get_rank()


def get_world_size():
    if not is_dist_available_and_initialized():
        return 1
    return torch.distributed.get_world_size()


def is_main_process():
    return get_rank() == 0


def save_on_master(*args, **kwargs):
    if is_main_process():
        torch.save(*args, **kwargs)


def warp_model(
    model: torch.nn.Module,
    sync_bn: bool = False,
    dist_mode: str = "ddp",
    find_unused_parameters: bool = False,
    compile: bool = False,
    compile_mode: str = "reduce-overhead",
    **kwargs,
):
    if is_dist_available_and_initialized():
        # Check if we're using GPU first
        using_gpu = next(model.parameters()).is_cuda

        # Only apply SyncBatchNorm if using GPU
        if sync_bn and using_gpu:
            model = nn.SyncBatchNorm.convert_sync_batchnorm(model)
        else:
            LOGGER.warning("SyncBatchNorm is not applied because GPU is not available.")

        if dist_mode == "dp":
            if using_gpu:
                rank = get_rank()
                model = DP(model, device_ids=[rank], output_device=rank)
            else:
                model = DP(model)
        elif dist_mode == "ddp":
            if using_gpu:
                rank = get_rank()
                model = DDP(model, device_ids=[rank], output_device=rank, find_unused_parameters=find_unused_parameters)
            else:
                model = DDP(model, find_unused_parameters=find_unused_parameters)
        else:
            raise AttributeError("Invalid dist_mode")

    if compile:
        model = torch.compile(model, mode=compile_mode)

    return model


def de_model(model):
    return de_parallel(de_complie(model))


def warp_loader(loader, shuffle=False):
    if is_dist_available_and_initialized():
        sampler = DistributedSampler(
            loader.dataset,
            shuffle=shuffle,
        )

        # Create new dataloader with distributed sampler
        loader = DataLoader(
            dataset=loader.dataset,
            batch_size=loader.batch_size,
            sampler=sampler,
            drop_last=loader.drop_last,
            num_workers=loader.num_workers,
            collate_fn=loader.collate_fn,
            pin_memory=loader.pin_memory,
            multiprocessing_context="fork",  # Use fork context for better compatibility
        )
    return loader


def is_parallel(model) -> bool:
    # Returns True if model is of type DP or DDP
    return type(model) in (torch.nn.parallel.DataParallel, torch.nn.parallel.DistributedDataParallel)


def de_parallel(model) -> nn.Module:
    # De-parallelize a model: returns single-GPU model if model is of type DP or DDP
    return model.module if is_parallel(model) else model


def reduce_dict(data, avg=True):
    """
    Args
        data dict: input, {k: v, ...}
        avg bool: true
    """
    world_size = get_world_size()
    if world_size < 2:
        return data

    with torch.no_grad():
        keys, values = [], []
        for k in sorted(data.keys()):
            keys.append(k)
            values.append(data[k])

        values = torch.stack(values, dim=0)
        torch.distributed.all_reduce(values)

        if avg is True:
            values /= world_size

        return {k: v for k, v in zip(keys, values)}


def all_gather(data):
    """
    Run all_gather on arbitrary picklable data (not necessarily tensors)
    Args:
        data: any picklable object
    Returns:
        list[data]: list of data gathered from each rank
    """
    world_size = get_world_size()
    if world_size == 1:
        return [data]
    data_list = [None] * world_size
    torch.distributed.all_gather_object(data_list, data)
    return data_list


def sync_time():
    """sync_time"""
    if torch.cuda.is_available():
        torch.cuda.synchronize()

    return time.time()


def setup_seed(seed: int, deterministic=False):
    """setup_seed for reproducibility
    torch.manual_seed(3407) is all you need. https://arxiv.org/abs/2109.08203
    """
    seed = seed + get_rank()
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)

    if torch.cuda.is_available():
        torch.cuda.manual_seed_all(seed)

    # memory will be large when setting deterministic to True
    if torch.backends.cudnn.is_available() and deterministic:
        torch.backends.cudnn.deterministic = True


# for torch.compile
def check_compile():
    import torch
    import warnings

    gpu_ok = False
    if torch.cuda.is_available():
        device_cap = torch.cuda.get_device_capability()
        if device_cap in ((7, 0), (8, 0), (9, 0)):
            gpu_ok = True
    if not gpu_ok:
        warnings.warn("GPU is not NVIDIA V100, A100, or H100. Speedup numbers may be lower " "than expected.")
    return gpu_ok


def is_compile(model):
    import torch._dynamo

    return type(model) in (torch._dynamo.OptimizedModule,)


def de_complie(model):
    return model._orig_mod if is_compile(model) else model


def infer_ddp_devices(device: str = None):
    """
    Get the device ids for setting up ddp. User can specify the devices in following ways:
    - "cuda" - Use 1st GPU
    - "cuda:0" or "cuda:1" etc. - Use specific GPU
    - 0 or 1 etc. - Use specific GPU
    - [0, 1] etc. - Use multiple GPUs
    - "0,1,2" etc. - Use multiple GPUs specified as comma-separated string (e.g. "0,1,2" -> [0,1,2])
    - "cuda:all" - Use all GPUs
    """
    if device is None:
        return [0] if torch.cuda.is_available() else ["cpu"]
    if device == "cpu":
        return ["cpu"]
    if device == "cuda":
        return [0]
    if isinstance(device, (int, str)):
        # Handle both integer inputs and string inputs like "0", "1" and "0,1,2"
        if str(device).isdigit():
            return [int(device)]
        # Handle comma-separated device string like "0,1,2"
        if isinstance(device, str) and "," in device:
            try:
                return [int(d.strip()) for d in device.split(",")]
            except ValueError:
                raise ValueError(f"Invalid comma-separated device format: {device}")
        # Handle "cuda:X" format
        if device.startswith("cuda:"):
            if device == "cuda:all":
                return list(range(torch.cuda.device_count()))
            try:
                return [int(device.split(":")[1])]
            except (IndexError, ValueError):
                raise ValueError(f"Invalid CUDA device format: {device}")
    # Handle list of devices
    if isinstance(device, list):
        return [int(d) if isinstance(d, str) and d.isdigit() else d for d in device]

    raise ValueError(f"Invalid device specification: {device}")
