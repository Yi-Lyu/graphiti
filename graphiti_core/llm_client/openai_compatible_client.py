"""
Copyright 2024, Zep Software, Inc.

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
"""

import logging
import typing
import json
from typing import ClassVar

import openai
from openai import AsyncOpenAI
from openai.types.chat import ChatCompletionMessageParam
from pydantic import BaseModel

from ..prompts.models import Message
from .client import LLMClient
from .config import LLMConfig
from .errors import RateLimitError, RefusalError

logger = logging.getLogger(__name__)

DEFAULT_MODEL = 'deepseek'


class OpenAICompatibleClient(LLMClient):
    """
    OpenAICompatibleClient is a client class for interacting with OpenAI's language models.

    This class extends the LLMClient and provides methods to initialize the client,
    get an embedder, and generate responses from the language model.

    Attributes:
        client (AsyncOpenAI): The OpenAI client used to interact with the API.
        model (str): The model name to use for generating responses.
        temperature (float): The temperature to use for generating responses.
        max_tokens (int): The maximum number of tokens to generate in a response.

    Methods:
        __init__(config: LLMConfig | None = None, cache: bool = False, client: typing.Any = None):
            Initializes the OpenAICompatibleClient with the provided configuration, cache setting, and client.

        _generate_response(messages: list[Message]) -> dict[str, typing.Any]:
            Generates a response from the language model based on the provided messages.
    """

    # Class-level constants
    MAX_RETRIES: ClassVar[int] = 2

    def __init__(
        self, config: LLMConfig | None = None, cache: bool = False, client: typing.Any = None
    ):
        """
        Initialize the OpenAICompatibleClient with the provided configuration, cache setting, and client.

        Args:
            config (LLMConfig | None): The configuration for the LLM client, including API key, model, base URL, temperature, and max tokens.
            cache (bool): Whether to use caching for responses. Defaults to False.
            client (Any | None): An optional async client instance to use. If not provided, a new AsyncOpenAI client is created.

        """
        # removed caching to simplify the `generate_response` override
        if cache:
            raise NotImplementedError('Caching is not implemented for OpenAI')

        if config is None:
            config = LLMConfig()

        super().__init__(config, cache)

        if client is None:
            self.client = AsyncOpenAI(api_key=config.api_key, base_url=config.base_url)
        else:
            self.client = client

    async def _generate_response(
        self, messages: list[Message], response_model: type[BaseModel] | None = None
    ) -> dict[str, typing.Any]:
        print(f"messages: {messages}")
        openai_messages: list[ChatCompletionMessageParam] = []
        for m in messages:
            m.content = self._clean_input(m.content)
            if m.role == 'user':
                openai_messages.append({'role': 'user', 'content': m.content})
            elif m.role == 'system':
                openai_messages.append({'role': 'system', 'content': m.content})

        print(f"openai_messages: {openai_messages}")
        try:
            # 检查是否使用 DeepSeek 模型
            is_deepseek = 'deepseek' in (self.model or DEFAULT_MODEL).lower()

            completion_params = {
                'model': self.model or DEFAULT_MODEL,
                'messages': openai_messages,
                'temperature': self.temperature,
                'max_tokens': self.max_tokens,
            }

            response = await self.client.chat.completions.create(**completion_params)

            response_content = response.choices[0].message.content

            print(f"response_content: {response_content}")

            # 对于 DeepSeek 模型，手动解析响应内容
            if is_deepseek and response_model:
                try:
                    # 尝试将响应解析为 JSON
                    parsed_content = json.loads(response_content)
                    return parsed_content
                except json.JSONDecodeError:
                    # 如果不是 JSON 格式，返回原始内容
                    return {'content': response_content}

            # 对于其他模型，返回原始内容
            print(f"response.choices[0].message.model_dump(): {response.choices[0].message.model_dump()}")

            return response.choices[0].message.model_dump()

        except openai.BadRequestError as e:
            logger.error(f"Bad request error: {str(e)}")
            raise
        except Exception as e:
            logger.error(f"Error in generating LLM response: {str(e)}")
            raise

    async def generate_response(
        self, messages: list[Message], response_model: type[BaseModel] | None = None
    ) -> dict[str, typing.Any]:
        retry_count = 0
        last_error = None

        while retry_count <= self.MAX_RETRIES:
            try:
                response = await self._generate_response(messages, response_model)
                return response
            except (RateLimitError, RefusalError):
                # These errors should not trigger retries
                raise
            except (openai.APITimeoutError, openai.APIConnectionError, openai.InternalServerError):
                # Let OpenAI's client handle these retries
                raise
            except Exception as e:
                last_error = e

                # Don't retry if we've hit the max retries
                if retry_count >= self.MAX_RETRIES:
                    logger.error(f'Max retries ({self.MAX_RETRIES}) exceeded. Last error: {e}')
                    raise

                retry_count += 1

                # Construct a detailed error message for the LLM
                error_context = (
                    f'The previous response attempt was invalid. '
                    f'Error type: {e.__class__.__name__}. '
                    f'Error details: {str(e)}. '
                    f'Please try again with a valid response, ensuring the output matches '
                    f'the expected format and constraints.'
                )

                error_message = Message(role='user', content=error_context)
                messages.append(error_message)
                logger.warning(
                    f'Retrying after application error (attempt {retry_count}/{self.MAX_RETRIES}): {e}'
                )

        # If we somehow get here, raise the last error
        raise last_error or Exception('Max retries exceeded with no specific error')