"""Native guard classification executed on the workbench, never brain fallback."""

import inspect


def reference_reader_program():
    # Separate container process has no live SQLite connections: the native unlocked
    # reader preserves file classification without a brain-side SQLite descriptor.
    from importlib import import_module

    guard = import_module("cron.lifecycle_guard")
    constants = [
        "_BINARY_MAGICS",
        "_BINARY_SNIFF_BYTES",
        "_MAX_REFERENCED_SCRIPT_BYTES",
        "_CLOUD_PLACEHOLDER_MARKERS",
    ]
    functions = [
        "_capped_read_limit",
        "_resolve_lenient",
        "_is_cloud_placeholder_path",
        "_on_cloud_path",
        "_has_binary_magic",
        "_read_referenced_script_unlocked",
    ]
    source = "import os,stat,sys,json\nfrom pathlib import Path\nfrom typing import Optional\n"
    source += (
        "\n".join(name + "=" + repr(getattr(guard, name)) for name in constants) + "\n"
    )
    source += "\n".join(inspect.getsource(getattr(guard, name)) for name in functions)
    source += "\np=Path(sys.argv[1]).expanduser(); text,unsafe=_read_referenced_script_unlocked(p,max_bytes=int(sys.argv[2]))\n"
    source += "print(json.dumps({'text':text,'unsafe':unsafe,'exists':p.exists(),'resolved_path':str(p.resolve())}))\n"
    return source
