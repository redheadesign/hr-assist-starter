"""Build an allowlisted, checked ZIP without personal data or Git history."""
import hashlib
import zipfile
from workspace import ROOT, local_path
from privacy_guard import allowed_files, check, git


def build():
    findings = check(index=True, history=True)
    if findings:
        raise ValueError('Privacy check failed: ' + '; '.join(findings))
    names = sorted(allowed_files(ROOT))
    payloads = {}
    for name in names:
        path = ROOT / name
        if path.is_symlink() or not path.resolve().is_relative_to(ROOT.resolve()):
            raise ValueError('Unsafe release path: ' + name)
        payload = git('show', ':' + name)
        working = path.read_bytes()
        if working.replace(b'\r\n', b'\n') != payload.replace(b'\r\n', b'\n'):
            raise ValueError('Stage and review current changes before packaging: ' + name)
        payloads[name] = payload
    output = local_path('artifacts/hr-assist-starter.zip')
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as archive:
        for name, payload in payloads.items():
            info = zipfile.ZipInfo('hr-assist-starter/' + name, (2026, 9, 5, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, payload)
    with zipfile.ZipFile(output) as archive:
        if archive.testzip() is not None or len(archive.namelist()) != len(payloads):
            raise ValueError('Archive verification failed')
    print(output)
    print('SHA256 ' + hashlib.sha256(output.read_bytes()).hexdigest())


if __name__ == '__main__':
    build()
