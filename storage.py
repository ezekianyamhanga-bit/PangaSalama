import io
import os
import tempfile
import uuid
from pathlib import Path, PurePosixPath
from PIL import Image, ImageOps, UnidentifiedImageError
from starlette.datastructures import UploadFile


MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_IMAGE_DIMENSION = 2400
ALLOWED_FORMATS = {"JPEG", "PNG", "WEBP"}
LOCAL_STORAGE_ROOT = Path(os.getenv(
    "LOCAL_IMAGE_STORAGE_PATH", Path(__file__).resolve().parent / "private_uploads")).resolve()


class InvalidImage(ValueError):
    pass


def process_image(upload: UploadFile) -> tuple[bytes, bytes]:
    upload.file.seek(0, os.SEEK_END)
    size = upload.file.tell()
    upload.file.seek(0)
    if size <= 0 or size > MAX_IMAGE_BYTES:
        raise InvalidImage("Image must be non-empty and no larger than 5 MB")

    try:
        with Image.open(upload.file) as image:
            if image.format not in ALLOWED_FORMATS:
                raise InvalidImage("Only JPEG, PNG, and WebP images are accepted")
            if getattr(image, "is_animated", False):
                raise InvalidImage("Animated images are not accepted")
            image.verify()
        upload.file.seek(0)
        with Image.open(upload.file) as image:
            if image.format not in ALLOWED_FORMATS:
                raise InvalidImage("Only JPEG, PNG, and WebP images are accepted")
            if image.width * image.height > 20_000_000:
                raise InvalidImage("Image dimensions are too large")
            image = ImageOps.exif_transpose(image).convert("RGB")
            full_image = image.copy()
            full_image.thumbnail((MAX_IMAGE_DIMENSION, MAX_IMAGE_DIMENSION), Image.Resampling.LANCZOS)
            thumbnail = image.copy()
            thumbnail.thumbnail((480, 360), Image.Resampling.LANCZOS)
            full_output = io.BytesIO()
            thumbnail_output = io.BytesIO()
            full_image.save(full_output, format="WEBP", quality=80, method=6)
            thumbnail.save(thumbnail_output, format="WEBP", quality=72, method=6)
            return full_output.getvalue(), thumbnail_output.getvalue()
    except InvalidImage:
        raise
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as error:
        raise InvalidImage("The uploaded file is not a valid supported image") from error


def _s3_client():
    import boto3

    options = {"region_name": os.getenv("S3_REGION")}
    endpoint = os.getenv("S3_ENDPOINT_URL")
    if endpoint:
        options["endpoint_url"] = endpoint
    return boto3.client("s3", **options)


def store_image(data: bytes) -> str:
    key = f"property-images/{uuid.uuid4().hex}.webp"
    backend = os.getenv("IMAGE_STORAGE_BACKEND", "local").strip().lower()
    if backend == "s3":
        _s3_client().put_object(
            Bucket=os.environ["S3_BUCKET"],
            Key=key,
            Body=data,
            ContentType="image/webp",
            CacheControl="private, no-store",
        )
    elif backend == "local" and os.getenv("APP_ENV", "development") != "production":
        target = LOCAL_STORAGE_ROOT / key
        target.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(prefix=".upload-", dir=str(target.parent))
        try:
            with os.fdopen(descriptor, "wb") as file:
                file.write(data)
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary_name, target)
        finally:
            if os.path.exists(temporary_name):
                os.unlink(temporary_name)
    else:
        raise RuntimeError("Image storage must be S3 in production or local outside production")
    return key


def read_image(key: str) -> bytes:
    parts = PurePosixPath(key).parts
    if (len(parts) != 2 or parts[0] != "property-images" or
            not parts[1].endswith(".webp") or "\\" in key or ".." in parts):
        raise ValueError("Invalid image storage key")
    backend = os.getenv("IMAGE_STORAGE_BACKEND", "local").strip().lower()
    if backend == "s3":
        from botocore.exceptions import ClientError

        try:
            response = _s3_client().get_object(Bucket=os.environ["S3_BUCKET"], Key=key)
        except ClientError as error:
            code = error.response.get("Error", {}).get("Code")
            if code in ("NoSuchKey", "404", "NotFound"):
                raise FileNotFoundError(key) from error
            raise
        try:
            return response["Body"].read()
        finally:
            response["Body"].close()
    if backend == "local" and os.getenv("APP_ENV", "development") != "production":
        target = (LOCAL_STORAGE_ROOT / key).resolve()
        if LOCAL_STORAGE_ROOT.resolve() not in target.parents:
            raise ValueError("Invalid image storage key")
        return target.read_bytes()
    raise RuntimeError("Image storage must be S3 in production or local outside production")


def delete_image(key: str) -> None:
    parts = PurePosixPath(key).parts
    if (len(parts) != 2 or parts[0] != "property-images" or
            not parts[1].endswith(".webp") or "\\" in key or ".." in parts):
        raise ValueError("Invalid image storage key")
    backend = os.getenv("IMAGE_STORAGE_BACKEND", "local").strip().lower()
    if backend == "s3":
        _s3_client().delete_object(Bucket=os.environ["S3_BUCKET"], Key=key)
    elif backend == "local" and os.getenv("APP_ENV", "development") != "production":
        target = (LOCAL_STORAGE_ROOT / key).resolve()
        if LOCAL_STORAGE_ROOT.resolve() not in target.parents:
            raise ValueError("Invalid image storage key")
        target.unlink(missing_ok=True)
    else:
        raise RuntimeError("Image storage must be S3 in production or local outside production")
