import io
import warnings
from contextlib import contextmanager

import torch


class torch_global:
    device = torch.device('cuda:0' if torch.cuda.is_available() else 'cpu')

    class set_device(object):
        def __init__(self, device, error='ignore'):
            try:
                count = torch.cuda.device_count()
                print('Available CUDA devices', count)
            except:
                print('No available CUDA devices')
            self.previous = torch_global.device
            try:
                new_device = torch.device(device) if isinstance(device, str) else device
                torch.as_tensor([0]).to(new_device)
            except:
                msg = f"Device {device} is not available"
                if error == "ignore":
                    print(msg)
                else:
                    raise Exception(msg)
            else:
                torch_global.device = new_device
            print('Current device', torch_global.device)

        def __enter__(self):
            pass

        def __exit__(self, exc_type, exc_val, exc_tb):
            torch_global.device = self.previous


@contextmanager
def evaluating(*nets):
    """Temporarily switch to evaluation mode."""
    were_training = [net.training for net in nets]
    try:
        for net in nets:
            net.eval()
        yield (nets if len(nets) > 1 else nets[0])
    finally:
        for net, was_training in zip(nets, were_training):
            if was_training:
                net.train()


@contextmanager
def training(*nets):
    """Temporarily switch to training mode."""
    were_training = [net.training for net in nets]
    try:
        for net in nets:
            net.train()
        yield (nets if len(nets) > 1 else nets[0])
    finally:
        for net, was_training in zip(nets, were_training):
            if not was_training:
                net.train()


class freeze:
    def __init__(self, net, fn=None):
        """Temporarily switch to evaluation mode."""
        was_frozen = []
        self.params = net.parameters() if hasattr(net, 'parameters') else net
        for i, param in enumerate(self.params):
            was_frozen.append(not param.requires_grad)
            if fn is None or fn(param):
                param.requires_grad = False
        self.was_frozen = was_frozen

    def __enter__(self):
        pass

    def __exit__(self, exc_type, exc_val, exc_tb):
        for i, param in enumerate(self.params):
            if not self.was_frozen[i]:
                param.requires_grad = True


class unfreeze:
    def __init__(self, net, fn=None):
        """Temporarily switch to evaluation mode."""
        was_frozen = []
        self.params = net.parameters() if hasattr(net, 'parameters') else net
        for i, param in enumerate(self.params):
            was_frozen.append(not param.requires_grad)
            if fn is None or fn(param):
                param.requires_grad = True
        self.was_frozen = was_frozen

    def __enter__(self):
        pass

    def __exit__(self, exc_type, exc_val, exc_tb):
        for i, param in enumerate(self.params):
            if self.was_frozen[i]:
                param.requires_grad = False


class Identity(torch.nn.Module):
    def forward(self, x, *args, **kwargs):
        return x


subset_list = []


@contextmanager
def no_subset():
    global subset_list
    redos = [undo_subset() for undo_subset in list(subset_list)]
    yield
    subset_list = [redo() for redo in redos]


def slice_parameters(obj, names, indexer, optimizer):
    subset_module_params = {}

    def do_subset():
        subset_module_params.clear()
        for module_param_name in names:
            module_param = getattr(obj, module_param_name)
            if module_param is None:
                continue

            subset_module_param = torch.nn.Parameter(module_param[indexer], requires_grad=module_param.requires_grad)
            optimizer_saved_state = None
            optimizer_subset_state = None
            if optimizer is not None and optimizer.state[module_param]:
                optimizer_saved_state = optimizer.state[module_param]
                for group in optimizer.param_groups:
                    group['params'] = [subset_module_param if x is module_param else x for x in group['params']]
                optimizer_subset_state = {}
                for optim_param_name, optim_param in optimizer_saved_state.items():
                    if hasattr(optim_param, 'shape') and optim_param.shape == module_param.shape:
                        optimizer_subset_state[optim_param_name] = optim_param[indexer]
                    else:
                        optimizer_subset_state[optim_param_name] = optim_param
                optimizer.state[subset_module_param] = optimizer_subset_state
                del optimizer.state[module_param]

            subset_module_params[module_param_name] = (subset_module_param,
                                                       module_param.device,
                                                       module_param,  # .detach().cpu(),
                                                       optimizer_saved_state,
                                                       optimizer_subset_state)
            setattr(obj, module_param_name, subset_module_param)

        subset_list.append(undo_subset)
        return undo_subset

    def undo_subset():
        for module_param_name, (subset_module_param,
                                device,
                                module_param_detached,
                                optimizer_saved_state,
                                optimizer_subset_state) in subset_module_params.items():

            module_param_detached.data[indexer] = subset_module_param.detach()
            restored_param = module_param_detached  # torch.nn.Parameter(module_param_detached.to(device), requires_grad=subset_module_param.requires_grad)

            # Update old embeddings with new ones

            if optimizer_saved_state is not None:
                for group in optimizer.param_groups:
                    group['params'] = [restored_param if x is subset_module_param else x for x in group['params']]

                for optim_param_name, optim_param in optimizer_subset_state.items():
                    if hasattr(optim_param, 'shape') and optim_param.shape == subset_module_param.shape:
                        subset_param = optimizer_subset_state[optim_param_name]
                        optimizer_subset_state[optim_param_name] = optimizer_saved_state[optim_param_name]
                        optimizer_subset_state[optim_param_name][indexer] = subset_param
                    optimizer.state[restored_param] = optimizer_subset_state
                del optimizer.state[subset_module_param]
            setattr(obj, module_param_name, restored_param)

        subset_list.remove(undo_subset)

        return do_subset

    return do_subset()


def torch_clone(obj, device=None):
    bio = io.BytesIO()
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", "Couldn't retrieve source code for")
        torch.save(obj, bio)
    bio.seek(0)
    return torch.load(bio, map_location=torch_global.device if device is None else device)


def extract_slices(sequences, flat_begins, flat_ends, flat_sample_idx):
    if isinstance(sequences, torch.Tensor):
        num, device = sequences.shape[1], sequences.device
    elif isinstance(sequences, (list, tuple)):
        num, device = sequences[0].shape[1], sequences[0].device
    elif isinstance(sequences, dict):
        num, device = next(iter(sequences.values())).shape[1], next(iter(sequences.values())).device
    else:
        raise Exception("Can only extract slices from Tensor, list of Tensor or dict of (any, Tensor)")

    if len(flat_ends):
        mention_length = (flat_ends - flat_begins).max().item()
    else:
        mention_length = 0
    mentions_from_sequences_col_indexer = torch.min(torch.arange(mention_length).unsqueeze(0) + flat_begins.unsqueeze(1),
                                                    torch.tensor(num, device=device) - 1)
    mentions_from_sequences_row_indexer = flat_sample_idx.unsqueeze(1)
    # token_id_[]
    mask = mentions_from_sequences_col_indexer < flat_ends.unsqueeze(1)
    if isinstance(sequences, torch.Tensor):
        return sequences[mentions_from_sequences_row_indexer, mentions_from_sequences_col_indexer], mask
    elif isinstance(sequences, (list, tuple)):
        return [seq[mentions_from_sequences_row_indexer, mentions_from_sequences_col_indexer] for seq in sequences], mask
    elif isinstance(sequences, dict):
        return {k: seq[mentions_from_sequences_row_indexer, mentions_from_sequences_col_indexer] for k, seq in sequences.items()}, mask
    print("Cannot be here, already raised and exception")

