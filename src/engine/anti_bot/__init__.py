"""
engine.anti_bot — 反检测子包根。

聚合代理轮换、浏览器指纹、验证码检测与求解四条能力线的公开接口。
行为模拟（behavior/）和隐身后端（stealth/）各由其子包 __init__.py
单独声明，需要时直接从子包导入。
"""

from src.engine.anti_bot.challenge_solver import ChallengeDetector, ChallengeSolver
from src.engine.anti_bot.fingerprint import FingerprintGenerator, FingerprintInjector
from src.engine.anti_bot.proxy_rotator import ProxyRotator

__all__ = [
    "ProxyRotator",
    "FingerprintGenerator",
    "FingerprintInjector",
    "ChallengeDetector",
    "ChallengeSolver",
]
