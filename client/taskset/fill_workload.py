import json
from pathlib import Path
"""
批量录入json格式的方法，输入问题.txt，每一行一个问题。输出：workload.json<UNK>
"""

TXT_PATH = Path("nq_task.txt")
TEMPLATE_JSON_PATH = Path("workload_example.json")
OUTPUT_JSON_PATH = Path("workload_nq.json")


def read_questions(txt_path: Path) -> list[str]:
    """读取 txt 中的问题，每行一个，自动去除空行。"""
    with txt_path.open("r", encoding="utf-8") as f:
        questions = [line.strip() for line in f if line.strip()]
    return questions


def load_template(json_path: Path) -> dict:
    """读取模板 JSON。"""
    with json_path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    return data


def build_requests(questions: list[str]) -> list[dict]:
    """根据问题列表构建 requests。"""
    requests = []
    for idx, question in enumerate(questions, start=1):
        item = {
            "name": f"q{idx}",
            "messages": [
                {
                    "role": "user",
                    "content": question
                }
            ]
        }
        requests.append(item)
    return requests


def main():
    if not TXT_PATH.exists():
        raise FileNotFoundError(f"找不到文件: {TXT_PATH}")

    if not TEMPLATE_JSON_PATH.exists():
        raise FileNotFoundError(f"找不到文件: {TEMPLATE_JSON_PATH}")

    questions = read_questions(TXT_PATH)
    if not questions:
        raise ValueError("txt 文件中没有读取到有效问题，请检查内容是否为空。")

    data = load_template(TEMPLATE_JSON_PATH)

    if not isinstance(data, dict):
        raise ValueError("模板 JSON 顶层结构应为对象。")

    data["requests"] = build_requests(questions)

    with OUTPUT_JSON_PATH.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    print(f"已生成: {OUTPUT_JSON_PATH}")
    print(f"共写入 {len(questions)} 条问题。")


if __name__ == "__main__":
    main()