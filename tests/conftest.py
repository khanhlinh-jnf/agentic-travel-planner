import os

os.environ["USE_MOCK_LLM"] = "true"
os.environ["USE_MOCK_TRAVEL_DATA"] = "true"
os.environ["USER_MEMORY_PATH"] = "./data/user_memory.test.json"
os.environ["LANGFUSE_ENABLED"] = "false"
os.environ["PROMPT_BACKEND"] = "local"
os.environ["PROMPT_AB_ENABLED"] = "false"
