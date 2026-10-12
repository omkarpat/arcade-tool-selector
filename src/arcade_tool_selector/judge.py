"""The judge: typed yes/no questions answered with probabilities (OpenAI Decisions API)."""

from openai import AsyncOpenAI

MODEL = "gpt-6-luna"


class DecisionsJudge:
    def __init__(self, model: str = MODEL):
        self.model = model
        self._client: AsyncOpenAI | None = None

    async def predicates(self, input: str, questions: dict[str, str]) -> dict[str, float]:
        """P(yes) per question name."""
        # Created on first use: a missing key then fails the call (closed), not the proxy's startup.
        self._client = self._client or AsyncOpenAI()
        response = await self._client.decisions.create(
            model=self.model,
            input=input,
            questions=[{"type": "predicate", "name": name, "instructions": text} for name, text in questions.items()],
        )
        return {answer.name: answer.probability for answer in response.answers}
