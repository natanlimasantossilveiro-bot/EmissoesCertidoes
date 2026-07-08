from certidoes_core.captcha.base import ResolvedorCaptcha
from certidoes_core.captcha.twocaptcha_provider import ResolvedorTwoCaptcha


def obter_resolvedor(provedor: str = "2captcha") -> ResolvedorCaptcha:
    """Ponto único de troca de provedor. Se um dia trocar de 2captcha pra
    outro serviço, só mexe aqui — nenhum worker precisa mudar."""
    if provedor == "2captcha":
        return ResolvedorTwoCaptcha()
    raise ValueError(f"Provedor de captcha não suportado: {provedor}")


__all__ = ["ResolvedorCaptcha", "obter_resolvedor"]
