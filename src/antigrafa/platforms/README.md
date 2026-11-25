# Platforms

A platform connects Antigrafa to a virtualization system. Back to the [main README](../../../README.md).

## Adding a platform

1. Create a folder `src/antigrafa/platforms/<name>/`.
2. Add `parse_settings(name, data)`: it checks the `[source.<name>]` section.
3. Add `create(name, settings)`: it returns a `Platform` (see [base.py](base.py)).
4. Register the `type` in `PLATFORMS`, in [\_\_init\_\_.py](__init__.py).

A `Platform` lists the guests, takes and deletes snapshots, and says how to read each disk.
Use [proxmox/](proxmox/) as the example.
