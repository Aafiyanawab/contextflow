import os
from openai import OpenAI
from dotenv import load_dotenv

load_dotenv()

client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
OPENAI_MODEL = "gpt-4o-mini"


def get_ai_response(enriched_prompt: str) -> dict:
    """
    Send enriched prompt to OpenAI and return response + token usage.
    """
    response = client.chat.completions.create(
        model=OPENAI_MODEL,
        messages=[
            {
                "role": "system",
                "content": "You are a senior cloud engineer assistant. "
                           "Provide specific, practical answers based on "
                           "the organizational context provided."
            },
            {
                "role": "user",
                "content": enriched_prompt
            }
        ],
        max_tokens=1000,
        temperature=0.3
    )

    return {
        "answer": response.choices[0].message.content,
        "input_tokens": response.usage.prompt_tokens,
        "output_tokens": response.usage.completion_tokens,
        "total_tokens": response.usage.total_tokens,
        "model": OPENAI_MODEL
    }


# ── Test ─────────────────────────────────────────────────
if __name__ == "__main__":
    from intent_engine import get_intent
    from context_builder import build_enriched_prompt

    # Simulated discovered context
    discovered = {
        "cloud": "aws",
        "iac": "terraform",
        "containerization": "docker",
        "cicd": "github_actions",
        "language": "python"
    }

    query = "Create Terraform code for Redis on ElastiCache"

    # Step 1: Detect intent
    intent_result = get_intent(query)
    print(f"Intent: {intent_result['intent'].upper()} "
          f"(method: {intent_result['method']})")

    # Step 2: Build enriched prompt
    enriched = build_enriched_prompt(
        query,
        intent_result['intent'],
        discovered
    )
    print(f"\nEnriched Prompt:\n{enriched}\n")
    print("=" * 50)

    # Step 3: Get AI response
    print("Calling OpenAI...")
    result = get_ai_response(enriched)

    print(f"\n🤖 Answer:\n{result['answer']}")
    print(f"\n📊 Tokens Used:")
    print(f"   Input:  {result['input_tokens']}")
    print(f"   Output: {result['output_tokens']}")
    print(f"   Total:  {result['total_tokens']}")