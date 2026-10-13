"""The judge: typed yes/no questions answered with probabilities (OpenAI Decisions API)."""

from openai import AsyncOpenAI

MODEL = "gpt-6-luna"


class DecisionsJudge:
    """The vetter's judge, backed by the OpenAI Decisions API."""

    def __init__(self, api_key: str):
        """A judge using this OpenAI key."""
        self.client = AsyncOpenAI(api_key=api_key)

    async def predicates(self, input: str, questions: dict[str, str]) -> dict[str, float]:
        """P(yes) per question name."""
        response = await self.client.decisions.create(
            model=MODEL,
            input=input,
            questions=[{"type": "predicate", "name": name, "instructions": text} for name, text in questions.items()],
        )
        return {answer.name: answer.probability for answer in response.answers}
