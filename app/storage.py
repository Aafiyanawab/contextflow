"""File storage behind one narrow interface — the AWS seam.

Local development writes under instance/uploads/. Production (and the
docker-compose stack, via MinIO) swaps in an S3-backed class with the
same four methods; nothing else in the app touches the filesystem. Keys
are generated from ids (never from user filenames), so path traversal is
impossible by construction.

`get_storage()` selects the backend from the environment:
    S3_BUCKET set   -> S3Storage  (real S3, or MinIO when S3_ENDPOINT_URL is set)
    otherwise       -> LocalStorage
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


class S3Storage:
    """S3-backed store with the same interface as LocalStorage.

    Works against real AWS S3 (no endpoint_url; credentials come from the
    environment or the EC2/EKS instance role) and against MinIO locally
    (endpoint_url + path-style addressing). boto3 is imported lazily so
    LocalStorage users don't need it installed.
    """

    def __init__(self, bucket, endpoint_url=None, region=None):
        import boto3
        from botocore.config import Config
        self.bucket = bucket
        # MinIO (and any non-AWS endpoint) needs path-style URLs.
        cfg = Config(s3={"addressing_style": "path"}) if endpoint_url else None
        self.client = boto3.client("s3", endpoint_url=endpoint_url,
                                   region_name=region, config=cfg)

    def save(self, key, data: bytes):
        self.client.put_object(Bucket=self.bucket, Key=key, Body=data)
        return key

    def read(self, key) -> bytes:
        obj = self.client.get_object(Bucket=self.bucket, Key=key)
        return obj["Body"].read()

    def delete(self, key):
        self.client.delete_object(Bucket=self.bucket, Key=key)

    def delete_workspace(self, workspace_id):
        """Delete every object under the workspace prefix (batched by 1000,
        the S3 delete_objects limit)."""
        prefix = f"{workspace_id}/"
        paginator = self.client.get_paginator("list_objects_v2")
        batch = []
        for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                batch.append({"Key": obj["Key"]})
                if len(batch) == 1000:
                    self.client.delete_objects(
                        Bucket=self.bucket, Delete={"Objects": batch})
                    batch = []
        if batch:
            self.client.delete_objects(
                Bucket=self.bucket, Delete={"Objects": batch})


def get_storage(local_base_dir):
    """Pick the storage backend from the environment. S3_BUCKET set ->
    S3 (or MinIO when S3_ENDPOINT_URL is set); otherwise local disk."""
    bucket = os.getenv("S3_BUCKET")
    if bucket:
        return S3Storage(
            bucket,
            endpoint_url=os.getenv("S3_ENDPOINT_URL") or None,
            region=os.getenv("AWS_REGION") or None,
        )
    return LocalStorage(local_base_dir)


def make_document_key(workspace_id, document_id, filename):
    """Storage key from ids plus the original extension only —
    the user-controlled filename never reaches the filesystem."""
    ext = os.path.splitext(filename)[1].lower()[:10]
    return f"{workspace_id}/{document_id}{ext}"
