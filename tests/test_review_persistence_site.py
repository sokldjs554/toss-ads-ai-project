"""stlite의 srcdoc 컴포넌트는 상대 JS 경로를 지원하지 않으므로 단일 HTML로 묶는다."""
import zipfile

from scripts.build_site import build_zip


def test_stlite_archive_has_a_self_contained_storage_component(tmp_path):
    target = tmp_path / "src.zip"
    build_zip(target)
    with zipfile.ZipFile(target) as archive:
        html = archive.read("ui/review_storage_frontend/index.html").decode("utf-8")
        assert 'from "./storage.js"' not in html
        assert "async function execute(command, namespace)" in html
        assert 'transaction.oncomplete = () => resolve(response)' in html
        assert 'ops/review_persistence.py' in archive.namelist()
