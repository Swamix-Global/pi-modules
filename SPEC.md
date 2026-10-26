# Techstack:
requests, uv, pydantic, fastapi, best dx friendly oss libraries known which can minimize the overall LOC with high elegance code.


## Features / Requirements:
- ctx engine is a middleman server
- accepts raw request, sends to inference host (LLM Provider), a minified context, to the LLM Provider
- adds gzip headers for faster transmit
- preserves, user,assistant,thinking at lower compression.
- drops any images or media which balloon context, or are uncompatible
- cache stays pristine, and high hit rate, this is core priority
- core priority, long running agent turns, with minimal overhead and strips unnecessary context which is stale.


## Definitions:
- Turn : user message to last successful assistant response is a turn, next user message is start of new turn

- TOOL CALL PAIR: a tool call and its output is tool call pair.


## Compression policy

- L1 : 
  - just drops tool call and tool results, until previous turn, current turn is preserved for better coherence. 

- L2 : 
  - Everything in Fast Mode + Assistant/Thinking messages are passed a compression stage.


## Local AI (SGlang)
- an sglang server with granite model is available, use it for summarizing the longer blocks 
- make a condenser.local_ai(system_prompt,user_message) -> str
- durable cache file system backed, so input  hash can fetched instantaneously


the backend package stays at five modules: config, transport, strategy, condenser, app, and least possible simple implementation, without excessive error handling. 
