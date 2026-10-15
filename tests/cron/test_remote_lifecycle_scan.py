"""Remote references use the execution namespace and working directory."""
from cron.lifecycle_guard import scan_gateway_lifecycle


def test_remote_scan_does_not_read_host_files(tmp_path):
    script = tmp_path / 'host.sh'
    script.write_text('hermes gateway restart')
    reads = []

    def reader(path, *, max_bytes):
        reads.append((path, max_bytes))
        return 'echo remote-safe'

    assert scan_gateway_lifecycle(
        f'bash {script}', read_remote_script=reader, remote_only=True,
    ) == (False, None)
    assert reads and reads[0][0] == str(script)


def test_nested_remote_references_keep_execution_cwd():
    files = {'/work/scripts/main.sh': 'bash helper', '/work/helper': 'hermes gateway restart'}
    reads = []

    def reader(path, *, max_bytes):
        reads.append(path)
        return files[path]

    unsafe, _ = scan_gateway_lifecycle(
        'bash scripts/main.sh', cwd='/work', read_remote_script=reader,
        remote_only=True, retain_cwd=True,
    )
    assert unsafe
    assert reads == ['/work/scripts/main.sh', '/work/helper']


def test_remote_home_is_resolved_by_reader_and_errors_are_refused():
    reads = []

    def reader(path, *, max_bytes):
        reads.append(path)
        raise OSError('unreachable')

    unsafe, refusal = scan_gateway_lifecycle(
        'bash ~/task.sh', read_remote_script=reader, remote_only=True,
    )
    assert unsafe and refusal
    assert reads == ['~/task.sh']
