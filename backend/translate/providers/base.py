from abc import ABC, abstractmethod


class BaseTranslator(ABC):
    """翻译 provider 抽象基类（决策 D3：统一接口）。

    所有 provider 必须实现 translate；translate_batch 提供默认实现，
    逐条调用 translate（子类可覆盖以做批量优化）。
    """

    #: provider 标识
    name: str = "base"
    #: 后端是否已真正实现（False 时前端应禁用，禁止静默失败）
    available: bool = False

    @abstractmethod
    async def translate(
        self, text: str, source_lang: str, target_lang: str, config: dict
    ) -> str:
        """翻译单段文本，返回译文。"""
        raise NotImplementedError

    async def translate_batch(
        self, texts: list, source_lang: str, target_lang: str, config: dict
    ) -> list:
        results = []
        for t in texts:
            if not (t or "").strip():
                results.append("")
            else:
                results.append(
                    await self.translate(t, source_lang, target_lang, config)
                )
        return results
