"""File storage behind one narrow interface — the AWS seam.

Local development writes under instance/uploads/. Production swaps in
an S3-backed class with the same four methods; nothing else in the app
touches the filesystem. Keys are generated from ids (never from user
filenames), so path traversal is impossible by construction.
"""
import os
import shutil


class LocalStorage:
    def __init__(self, base_dir):
        self.base_dir = base_dir

    def _path(self, key):
        return os.path.join(self.base_dir, *key.split("/"))

    def save(self, key, data: bytes):
        path = self._path(key)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(data)
        return key

    def read(self, key) -> bytes:
        with open(self._path(key), "rb") as f:
            return f.read()

    def delete(self, key):
        try:
            os.remove(self._path(key))
        except FileNotFoundError:
            pass

    def delete_workspace(self, workspace_id):
        """Remove every stored file for a workspace (workspace deletion)."""
        shutil.rmtree(os.path.join(self.base_dir, workspace_id),
                      ignore_errors=True)


def make_document_key(workspace_id, document_id, filename):
    """Storage key from ids plus the original extension only —
    the user-controlled filename never reaches the filesystem."""
    ext = os.path.splitext(filename)[1].lower()[:10]
    return f"{workspace_id}/{document_id}{ext}"
