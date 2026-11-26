# Platforms

A platform connects Antigrafa to a virtualization system, such as Proxmox VE.
Back to the [main README](../../../README.md).

## Adding a platform

1. Create a folder `src/antigrafa/platforms/<name>/`.
2. Add `parse_settings(name, data)`: it checks the `[source.<name>]` section of the configuration.
3. Add `create(name, settings)`: it returns a `Platform` (see [base.py](base.py)).
4. Register the platform's `type` in `PLATFORMS`, in [\_\_init\_\_.py](__init__.py).

A `Platform` does four things:

- list the VMs and containers;
- take a snapshot;
- delete a snapshot;
- say how to read each disk.

Use [proxmox/](proxmox/) as the example.
