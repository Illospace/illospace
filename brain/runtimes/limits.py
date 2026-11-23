"""The final request keeps the system rules and has no tools."""

LIMIT_INSTRUCTION = (
    "You reached the tool limit of this run. Answer now from the results that you have. "
    "Say plainly what you did not check."
)
LIMIT_RESULT = "The tool limit is reached. This tool did not run."
NO_FINAL_ANSWER = 'I reached my step limit before I had an answer. Ask again with a narrower question.'
