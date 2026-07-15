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
    async def resolver_recaptcha_enterprise(self, sitekey: str, url_pagina: str, invisible: bool = True) -> str:
        """reCAPTCHA Enterprise (diferente do v2 comum — usa pontuação de
        risco comportamental do Google, pode rejeitar mesmo com token
        válido). `invisible=True` pra widgets size:"invisible" (sem
        checkbox visível, executado via JS sob demanda)."""
        raise NotImplementedError

    @abstractmethod
    async def resolver_captcha_imagem(self, imagem_base64: str) -> str:
        """Pra captchas simples de texto/imagem (ex: alguns Atende.Net)."""
        raise NotImplementedError

    @abstractmethod
    async def resolver_turnstile(self, sitekey: str, url_pagina: str) -> str:
        """Cloudflare Turnstile (ex: MPF) — diferente de hCaptcha/reCAPTCHA,
        mas resolvido da mesma forma: token pronto pra injetar via callback
        que a própria página registrou em turnstile.render({callback})."""
        raise NotImplementedError
