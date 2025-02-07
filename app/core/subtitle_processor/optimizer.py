import difflib
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from typing import Dict

import retry
from openai import OpenAI

from .subtitle_config import (
    TRANSLATE_PROMPT,
    OPTIMIZER_PROMPT,
    REFLECT_TRANSLATE_PROMPT,
    SINGLE_TRANSLATE_PROMPT
)
from ..subtitle_processor.aligner import SubtitleAligner
from ..utils import json_repair
from ..utils.logger import setup_logger

logger = setup_logger("subtitle_optimizer")

BATCH_SIZE = 20
MAX_THREADS = 10
DEFAULT_MODEL = "gpt-4o-mini"


class SubtitleOptimizer:
    """A class for optimize and translating subtitles using OpenAI's API."""

    def __init__(self, model: str = DEFAULT_MODEL, summary_content="", thread_num=MAX_THREADS, batch_num=BATCH_SIZE,
                 target_language="Chinese", llm_result_logger: logging.Logger = None) -> None:
        base_url = os.getenv('OPENAI_BASE_URL')
        api_key = os.getenv('OPENAI_API_KEY')
        assert base_url and api_key, "环境变量 OPENAI_BASE_URL 和 OPENAI_API_KEY 必须设置"

        self.model = model
        self.client = OpenAI(base_url=base_url, api_key=api_key)

        self.summary_content = summary_content
        self.prompt = TRANSLATE_PROMPT
        self.target_language = target_language
        self.batch_num = batch_num
        self.thread_num = thread_num
        self.executor = ThreadPoolExecutor(max_workers=thread_num)  # 创建类级别的线程池
        self.llm_result_logger = llm_result_logger

    def stop(self):
        self.executor.shutdown(wait=False)

    @retry.retry(tries=2)
    def optimize(self, original_subtitle: Dict[int, str]) -> Dict[int, str]:
        """ Optimize the given subtitle. """
        logger.info(f"[+]正在优化字幕：{next(iter(original_subtitle))} - {next(reversed(original_subtitle))}")

        message = self._create_optimizer_message(original_subtitle)

        response = self.client.chat.completions.create(
            model=self.model,
            stream=False,
            messages=message)

        optimized_text = json_repair.loads(response.choices[0].message.content)

        for k, v in optimized_text.items():
            self.llm_result_logger.info(f"优化字幕：{original_subtitle[k]}")
            self.llm_result_logger.info(f"优化结果：{v}")
            self.llm_result_logger.info("===========")

        aligned_subtitle = repair_subtitle(original_subtitle, optimized_text)  # 修复字幕对齐问题

        return aligned_subtitle

    def optimizer_multi_thread(self, subtitle_json: Dict[int, str],
                               translate=False,
                               reflect=False,
                               callback=None):
        batch_num = self.batch_num
        items = list(subtitle_json.items())[:]
        chunks = [dict(items[i:i + batch_num]) for i in range(0, len(items), batch_num)]

        def process_chunk(chunk):
            if translate:
                try:
                    result = self.translate(chunk, reflect)
                except Exception as e:
                    logger.error(f"翻译失败，使用单条翻译：{e}")
                    result = self.translate_single(chunk)
            else:
                try:
                    result = self.optimize(chunk)
                except Exception as e:
                    logger.error(f"优化失败：{e}")
                    result = chunk
            if callback:
                callback(result)
            return result

        results = list(self.executor.map(process_chunk, chunks))

        # 合并结果
        optimizer_result = {k: v for result in results for k, v in result.items()}
        return optimizer_result

    @retry.retry(tries=2)
    def translate(self, original_subtitle: Dict[int, str], reflect=False) -> Dict[int, str]:
        """优化并翻译给定的字幕。"""
        if reflect:
            return self._reflect_translate(original_subtitle)
        else:
            return self._normal_translate(original_subtitle)

    def _reflect_translate(self, original_subtitle: Dict[int, str]):
        logger.info(f"[+]正在反思翻译字幕：{next(iter(original_subtitle))} - {next(reversed(original_subtitle))}")
        message = self._create_translate_message(original_subtitle)
        response = self.client.chat.completions.create(
            model=self.model,
            stream=False,
            messages=message,
            temperature=0.7)
        response_content = json_repair.loads(response.choices[0].message.content)
        optimized_text = {k: v["optimized_subtitle"] for k, v in response_content.items()}  # 字幕文本
        aligned_subtitle = repair_subtitle(original_subtitle, optimized_text)  # 修复字幕对齐问题
        # 在 translations 中查找对应的翻译  文本-翻译 映射
        translations = {item["optimized_subtitle"]: item["revised_translation"] for item in response_content.values()}
        translated_subtitle = {k: f"{v}\n{translations.get(v, ' ')}" for k, v in aligned_subtitle.items()}
        if self.llm_result_logger:
            for k, v in response_content.items():
                self.llm_result_logger.info(f"原字幕：{v['optimized_subtitle']}")
                self.llm_result_logger.info(f"翻译后字幕：{v['translation']}")
                self.llm_result_logger.info(f"反思后字幕：{v['revised_translation']}")
                self.llm_result_logger.info("===========")
        return translated_subtitle

    def _normal_translate(self, original_subtitle: Dict[int, str]):
        logger.info(f"[+]正在翻译字幕：{next(iter(original_subtitle))} - {next(reversed(original_subtitle))}")
        prompt = TRANSLATE_PROMPT.replace("[TargetLanguage]", self.target_language)
        message = [{"role": "system", "content": prompt},
                   {"role": "user",
                    "content": f"Please translate the input into {self.target_language}:\n<input>{str(original_subtitle)}</input>"}]
        response = self.client.chat.completions.create(
            model=self.model,
            stream=False,
            messages=message,
            temperature=0.7)
        response_content = json_repair.loads(response.choices[0].message.content)
        assert isinstance(response_content, dict) and len(response_content) == len(original_subtitle), "翻译结果错误"
        # logger.info(f"翻译结果：{next(iter(original_subtitle))} - {next(reversed(original_subtitle))}, 翻译条数：{len(response_content)}")
        translated_subtitle = {}
        original_list = list(original_subtitle.values())
        translated_list = list(response_content.values())
        for i, key in enumerate(original_subtitle.keys()):
            translated_subtitle[key] = f"{original_list[i]}\n{translated_list[i]}"

        return translated_subtitle

    def _create_translate_message(self, original_subtitle: Dict[int, str]):
        input_content = f"correct the original subtitles, and translate them into {self.target_language}:\n<input_subtitle>{str(original_subtitle)}</input_subtitle>"
        if self.summary_content:
            input_content += f"\nThe following is the relevant subtitle content, based on which the subtitles will be corrected, optimized, and translated:\n<summary>{self.summary_content}</summary>\n"
        example_input = f'correct the original subtitles and translate them into Chinese: {{"1": "If you\'re a developer", "2": "Then you probably cannot get around the Cursor IDE right now."}}'
        example_output = ('{"1": {"optimized_subtitle": "If you\'re a developer", "translate": "如果你是开发者", '
                          '"revise_suggestions": "the translation is accurate and fluent.", "revised_translate": '
                          '"如果你是开发者"}, "2": {"optimized_subtitle": "Then you probably cannot get around the Cursor IDE right '
                          'now.", "translate": "那么你现在可能无法绕开Cursor这款IDE。", "revise_suggestions": "The term \'绕开\' '
                          'feels awkward in this context. Consider using \'避开\' instead.", "revised_translate": '
                          '"那么你现在可能无法避开Cursor这款IDE。"}}')
        prompt = REFLECT_TRANSLATE_PROMPT.replace("[TargetLanguage]", self.target_language)
        message = [{"role": "system", "content": prompt},
                #    {"role": "user", "content": example_input},
                #    {"role": "assistant", "content": example_output},
                   {"role": "user", "content": input_content}]
        # print(prompt + "\n\n" + input_content)
        return message

    def _create_optimizer_message(self, original_subtitle):
        input_content = f"The following is the relevant subtitle content, based on which the subtitles will be corrected and optimized.:\n<input_subtitle>{str(original_subtitle)}</input_subtitle>"
        if self.summary_content:
            input_content += f"\nBelow is a summary of the subtitle content and related keywords:\n<summary>{self.summary_content}</summary>\n"
        example_input = '{"0": "调用现成的start方法","1": "才能成功开启现成啊","2": "而且stread这个类","3": "很多业务罗技和这个代码","4": "全部都给掺到一块去了"}'
        example_output = '{"0": "调用线程的start()方法","1": "才能成功开启线程","2": "而且Thread这个类","3": "很多业务逻辑和这个代码","4": "全部都给掺到一块去了"}'
        message = [{"role": "system", "content": OPTIMIZER_PROMPT},
                #    {"role": "user", "content": example_input},
                #    {"role": "assistant", "content": example_output},
                   {"role": "user", "content": input_content}]
        return message

    def translate_single(self, original_subtitle: Dict[int, str]) -> Dict[int, str]:
        """单条字幕翻译，用于在批量翻译失败时的备选方案"""
        translate_result = {}
        for key, value in original_subtitle.items():
            try:
                message = [{"role": "system",
                            "content": SINGLE_TRANSLATE_PROMPT.replace("[TargetLanguage]", self.target_language)},
                           {"role": "user", "content": value}]
                response = self.client.chat.completions.create(
                    model=self.model,
                    stream=False,
                    messages=message)
                translate = response.choices[0].message.content.replace("\n", "")
                translate_result[key] = f"{value}\n{translate}"
                logger.info(f"单条翻译结果: {translate_result[key]}")
            except Exception as e:
                logger.error(f"单条翻译失败: {e}")
                translate_result[key] = f"{value}\n "
        return translate_result


def repair_subtitle(dict1, dict2) -> Dict[int, str]:
    list1 = list(dict1.values())
    list2 = list(dict2.values())
    text_aligner = SubtitleAligner()
    aligned_source, aligned_target = text_aligner.align_texts(list1, list2)

    assert len(aligned_source) == len(aligned_target), "对齐后字幕长度不一致"
    # 验证是否匹配
    similar_list = calculate_similarity_list(aligned_source, aligned_target)
    if similar_list.count(True) / len(similar_list) >= 0.8:
        # logger.info(f"修复成功！序列匹配相似度：{similar_list.count(True) / len(similar_list):.2f}")
        start_id = next(iter(dict1.keys()))
        modify_dict = {str(int(start_id) + i): value for i, value in enumerate(aligned_target)}
        return modify_dict
    else:
        logger.error(f"修复失败！相似度：{similar_list.count(True) / len(similar_list):.2f}")
        logger.error(f"源字幕：{list1}")
        logger.error(f"目标字幕：{list2}")
        raise ValueError("Fail to repair.")


def is_similar(text1, text2, threshold=0.4):
    similarity = difflib.SequenceMatcher(None, text1, text2).ratio()
    return similarity >= threshold


def calculate_similarity_list(list1, list2, threshold=0.5):
    max_len = min(len(list1), len(list2))
    similar_list = [False] * max_len  # 初始化相似性列表

    for i in range(max_len):
        similar_list[i] = is_similar(list1[i], list2[i], threshold)

    return similar_list


if __name__ == "__main__":
    # os.environ['OPENAI_BASE_URL'] = 'https://api.gptgod.online/v1'
    # os.environ['OPENAI_API_KEY'] = 'sk-4StuHHm6Z1q0VcPHdPTUBdmKMsHW9JNZKe4jV7pJikBsGRuj'
    # MODEL = "gpt-4o-mini"
    os.environ['OPENAI_BASE_URL'] = 'https://api.turboai.one/v1'
    os.environ['OPENAI_API_KEY'] = 'sk-ZOCYCz5kexAS3X8JD3A33a5eB20f486eA26896798055F2C5'
    MODEL = "gpt-4o-mini"
