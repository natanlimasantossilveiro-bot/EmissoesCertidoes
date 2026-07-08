"""
Implementação usando a API do 2captcha. É a que você já usa no
SsaMonitorProcessos — só isolada aqui pra qualquer worker reaproveitar.
"""
import asyncio
from twocaptcha import TwoCaptcha  # pip install 2captcha-python

from certidoes_core.captcha.base import ResolvedorCaptcha
from certidoes_core.config import config


class ResolvedorTwoCaptcha(ResolvedorCaptcha):
    def __init__(self, api_key: str = None):
        self._cliente = TwoCaptcha(api_key or config.TWOCAPTCHA_API_KEY)

    async def resolver_recaptcha_v2(self, sitekey: str, url_pagina: str) -> str:
        resultado = await asyncio.to_thread(
            self._cliente.recaptcha, sitekey=sitekey, url=url_pagina
        )
        return resultado["code"]

    async def resolver_hcaptcha(self, sitekey: str, url_pagina: str) -> str:
        resultado = await asyncio.to_thread(
            self._cliente.hcaptcha, sitekey=sitekey, url=url_pagina
        )
        return resultado["code"]

    async def resolver_recaptcha_enterprise(self, sitekey: str, url_pagina: str, invisible: bool = True) -> str:
        resultado = await asyncio.to_thread(
            self._cliente.recaptcha,
            sitekey=sitekey,
            url=url_pagina,
            version="v2",
            enterprise=1,
            invisible=1 if invisible else 0,
        )
        return resultado["code"]

    async def resolver_captcha_imagem(self, imagem_base64: str) -> str:
        resultado = await asyncio.to_thread(self._cliente.normal, imagem_base64)
        return resultado["code"]
