"""运行核心：SEO 文案生成（ModelScope Qwen / DeepSeek）。"""

from __future__ import annotations

import json
import os
import time

from . import config as cfg
from .runtime import log
from .audio import download_file  # 未在此模块直接引用，但保持导入一致性
from .cover import (
    _get_modelscope_usage_token_pool,
    _run_text_task_with_model_fallback,
    _create_modelscope_openai_client,
    _extract_modelscope_chat_content,
    _strip_markdown_code_fences,
    _get_modelscope_text_model_sequence,
)


def auto_create_youtube_seo(book_name, book_desc, output_path, token):
    text_token_pool = _get_modelscope_usage_token_pool(token, "text")
    if not text_token_pool:
        raise ValueError("未提供 ModelScope 文字 Token，无法生成 SEO 文案。")

    log.info("【📝 AI文案大师】[%s] 分析书籍内容以撰写 YouTube SEO 最优化简介...", book_name)

    attempt = 0
    text_model_sequence = _get_modelscope_text_model_sequence()
    while True:
        attempt += 1

        def _generate_seo_dict(current_token, text_model):
            client = _create_modelscope_openai_client(current_token)

            system_prompt = """角色设定：
你现在是一位千万粉丝级别的 YouTube 运营专员与 SEO 大师。
你的任务是根据提供的【书名】和【内容简介】，为有声书视频精心打造一套高点击率（CTR）视频标题、引人入胜的描述、以及利于算法推荐的 #标签。

输出格式约束（必须严格遵守的铁律）：
你必须且只能返回一个合法的 JSON 格式对象字符串，绝对禁止输出任何多余的汉字解释、前言或者 Markdown 代码块标识！不要加上 ```json 这三个字！
JSON 必须严格有且只有以下三个 key：
{
  "title": "你设计的高吸引力长标题",
  "Description": "用Emoji点缀的带悬念和痛点的高转换率介绍词，长度大约 200 字。",
  "label": "#有声书 #个人成长 #认知刷新 等至少20个长短尾热门标签组"
}"""

            user_prompt = f"书名：[{book_name}]\n简介：[{book_desc}]"

            response = client.chat.completions.create(
                model=text_model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
            )

            llm_reply = _strip_markdown_code_fences(_extract_modelscope_chat_content(response))

            return json.loads(llm_reply)

        seo_dict, generation_errors = _run_text_task_with_model_fallback(
            task_label="SEO 文案生成",
            token_pool=text_token_pool,
            attempt=attempt,
            runner=_generate_seo_dict,
            model_sequence=text_model_sequence,
        )

        if seo_dict:
            with open(output_path, "w", encoding="utf-8") as f:
                json.dump(seo_dict, f, ensure_ascii=False, indent=2)

            log.info(
                "🎉 YouTube SEO 结构化脑暴文案 (JSON) 已于第 %d 次生成并提取保存为: %s",
                attempt,
                os.path.basename(output_path),
            )
            return True, seo_dict

        log.warning(
            "⚠️ SEO 文案生成第 %d 次失败：当前可用 token 全部未能生成可用结果。错误摘要：%s；系统将持续重试，直到成功为止。",
            attempt,
            " | ".join(generation_errors[-5:]) if generation_errors else "无",
        )
        time.sleep(min(30, 5 + attempt))