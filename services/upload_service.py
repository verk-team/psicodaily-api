from dotenv import load_dotenv
from io import BytesIO
from botocore.exceptions import BotoCoreError, ClientError
from PIL import Image, ImageOps, UnidentifiedImageError
from urllib.parse import quote
import boto3
import logging
import os

from services.erros import ErroNegocio

load_dotenv()
logger = logging.getLogger(__name__)

# O r2.dev não redimensiona pela URL, então a foto já sobe no tamanho de exibição.
TAMANHO_MAXIMO_FOTO = (512, 512)

class UploadService():
    def __init__(self):
        self.s3 = boto3.client(
            service_name = 's3',
            endpoint_url = os.getenv('ENDPOINT_URL'),
            aws_access_key_id = os.getenv('ACCESS_KEY_ID'),
            aws_secret_access_key = os.getenv('SECRET_ACCESS_KEY'),
            region_name = 'auto'
        )
        # URL publica do bucket, nao e segredo; o .env so precisa dela se o bucket mudar
        self.url_publica = os.getenv('R2_PUBLIC_URL', 'https://pub-b6945a8ffa914112b679b4f615712e92.r2.dev').rstrip('/')
        # F6: bucket sem acesso publico (arquivos do chat); so se le por URL assinada
        self.bucket_privado = os.getenv('R2_BUCKET_PRIVADO', 'psicodaily-objects')

    def upload_file(self, imagem, id_usuario):
        upload_name = f"usuarios/{id_usuario}.jpg"

        # Reabrir com o Pillow também garante que o arquivo é mesmo uma imagem.
        try:
            foto = ImageOps.exif_transpose(Image.open(imagem)).convert('RGB')
        except (UnidentifiedImageError, OSError):
            raise ErroNegocio('Não foi possível ler a imagem; envie outra foto JPG')
        foto.thumbnail(TAMANHO_MAXIMO_FOTO)
        arquivo = BytesIO()
        foto.save(arquivo, 'JPEG', quality=85, optimize=True)
        arquivo.seek(0)

        self._enviar(arquivo, 'psicodaily', upload_name, {
            'ContentType': 'image/jpeg',
            # A chave não muda ao trocar a foto, então o navegador revalida pelo ETag.
            'CacheControl': 'no-cache',
        })

    def url_foto_usuario(self, id_usuario):
        return f"{self.url_publica}/usuarios/{id_usuario}.jpg"

    def enviar_privado(self, conteudo, chave, tipo):
        self._enviar(BytesIO(conteudo), self.bucket_privado, chave, {'ContentType': tipo})

    def _enviar(self, arquivo, bucket, chave, extras):
        try:
            self.s3.upload_fileobj(arquivo, bucket, chave, ExtraArgs=extras)
        except (BotoCoreError, ClientError):
            # Chave sem permissao de escrita (AccessDenied) ou ENDPOINT_URL ausente: o log diz qual; o usuario so tenta depois
            logger.exception('Upload para o R2 falhou: %s/%s', bucket, chave)
            raise ErroNegocio('Não foi possível enviar o arquivo agora. Tente novamente em alguns minutos.', 503)

    def url_assinada(self, chave, nome, tipo, segundos):
        """URL de download que expira; gerada localmente, sem chamar o R2."""
        return self.s3.generate_presigned_url('get_object', ExpiresIn=segundos, Params={
            'Bucket': self.bucket_privado,
            'Key': chave,
            'ResponseContentType': tipo,
            'ResponseContentDisposition': f"attachment; filename*=UTF-8''{quote(nome)}",
        })

    def list_objects(self):
        res = self.s3.list_objects_v2(Bucket='psicodaily')
        for obj in res.get('Contents', []):
            print(f"Object: {obj}")

upload_service = UploadService()
