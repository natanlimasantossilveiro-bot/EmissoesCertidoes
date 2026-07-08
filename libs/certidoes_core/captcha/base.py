"""
Interface comum pra qualquer resolvedor de captcha. Cada worker declara
qual tipo de captcha o portal dele usa e pega a implementação certa,
sem precisar saber os detalhes de como o 2captcha (ou outro provedor)
funciona por dentro.
"""
from abc import ABC, abstractmethod


class ResolvedorCaptcha(ABC):
    """Toda implementação (2captcha, anti-captcha, etc.) herda daqui."""

    @abstractmethod
    async def resolver_recaptcha_v2(self, sitekey: str, url_pagina: str) -> str:
        """Retorna o token de resposta do reCAPTCHA v2, pronto pra injetar
        no campo g-recaptcha-response da página."""
        raise NotImplementedError

    @abstractmethod
    async def resolver_hcaptcha(self, sitekey: str, url_pagina: str) -> str:
        raise NotImplementedError

    @abstractmethod
    async def resolver_captcha_imagem(self, imagem_base64: str) -> str:
        """Pra captchas simples de texto/imagem (ex: alguns Atende.Net)."""
        raise NotImplementedError
