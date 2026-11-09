"""Arquivos no Cloudflare R2: fotos de perfil (bucket público) e anexos do chat (bucket privado).

O R2 funciona como o S3 da Amazon, então usamos a biblioteca boto3, que fala esse protocolo.
Bucket é uma "pasta" de arquivos na nuvem; cada arquivo é encontrado pela sua chave (o caminho dele).
"""
import logging
import os
from io import BytesIO
from urllib.parse import quote

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from dotenv import load_dotenv
from PIL import Image, ImageOps, UnidentifiedImageError

from services.erros import ErroNegocio

load_dotenv()
logger = logging.getLogger(__name__)

# O r2.dev não redimensiona pela URL, então a foto já sobe no tamanho de exibição.
TAMANHO_MAXIMO_FOTO = (512, 512)


class UploadService():
    def __init__(self):
        self.s3 = boto3.client(
            service_name='s3',
            endpoint_url=os.getenv('ENDPOINT_URL'),
            aws_access_key_id=os.getenv('ACCESS_KEY_ID'),
            aws_secret_access_key=os.getenv('SECRET_ACCESS_KEY'),
            region_name='auto'
        )
        # URL pública do bucket de fotos. Não é segredo; o .env só precisa dela se o bucket mudar
        self.url_publica = os.getenv('R2_PUBLIC_URL', 'https://pub-b6945a8ffa914112b679b4f615712e92.r2.dev').rstrip('/')
        # Bucket sem acesso público, para os arquivos do chat: só se lê por URL assinada (ver url_assinada)
        self.bucket_privado = os.getenv('R2_BUCKET_PRIVADO', 'psicodaily-objects')

    def upload_file(self, imagem, id_usuario):
        """Salva a foto de perfil em usuarios/<id>.jpg, já girada, reduzida e convertida para JPEG."""
        upload_name = f"usuarios/{id_usuario}.jpg"

        # Reabrir com o Pillow também garante que o arquivo é mesmo uma imagem.
        # exif_transpose gira a foto conforme o celular marcou (senão ela pode aparecer deitada)
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
        """Endereço público da foto. Não confere se o arquivo existe: quem nunca enviou foto recebe um link que dá 404."""
        return f"{self.url_publica}/usuarios/{id_usuario}.jpg"

    def enviar_privado(self, conteudo, chave, tipo):
        """Guarda bytes no bucket privado (anexos do chat)."""
        self._enviar(BytesIO(conteudo), self.bucket_privado, chave, {'ContentType': tipo})

    def _enviar(self, arquivo, bucket, chave, extras):
        try:
            self.s3.upload_fileobj(arquivo, bucket, chave, ExtraArgs=extras)
        except (BotoCoreError, ClientError):
            # Chave sem permissão de escrita (AccessDenied) ou ENDPOINT_URL ausente: o log diz qual;
            # o usuário só tenta de novo depois
            logger.exception('Upload para o R2 falhou: %s/%s', bucket, chave)
            raise ErroNegocio('Não foi possível enviar o arquivo agora. Tente novamente em alguns minutos.', 503)

    def url_assinada(self, chave, nome, tipo, segundos):
        """URL de download que expira; gerada localmente, sem chamar o R2.

        A assinatura no fim da URL prova que fomos nós que liberamos o arquivo, e só vale por `segundos`.
        """
        return self.s3.generate_presigned_url('get_object', ExpiresIn=segundos, Params={
            'Bucket': self.bucket_privado,
            'Key': chave,
            'ResponseContentType': tipo,
            # attachment: o navegador baixa o arquivo com o nome original
            'ResponseContentDisposition': f"attachment; filename*=UTF-8''{quote(nome)}",
        })


# Uma instância só para a API inteira: o cliente boto3 é criado uma vez, na importação
upload_service = UploadService()
