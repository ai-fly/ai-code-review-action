import requests
import re
import os
import json
from openai import OpenAI

# 从环境变量获取配置
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o")
GITHUB_EVENT_PATH = os.getenv("GITHUB_EVENT_PATH")

# 初始化 OpenAI 客户端
client = OpenAI(api_key=OPENAI_API_KEY, base_url="https://api.openai-prc.com/v1")

def get_pr_diff(pr_number, repo, headers):
    """获取 Pull Request 的 diff"""
    diff_url = f"https://api.github.com/repos/{repo}/pulls/{pr_number}"
    response = requests.get(diff_url, headers=headers, params={"accept": "application/vnd.github.diff"})
    if response.status_code == 200:
        return response.text
    else:
        raise Exception(f"Failed to fetch diff: {response.status_code}")

def parse_diff(diff):
    """解析 diff，提取文件、行号和代码块"""
    diff_lines = diff.splitlines()
    file_changes = []
    current_file = None
    current_hunk = None
    for line in diff_lines:
        if line.startswith("diff --git"):
            if current_file:
                file_changes.append(current_file)
            current_file = {"file": line.split()[-1][2:], "hunks": []}
        elif line.startswith("@@"):
            if current_hunk:
                current_file["hunks"].append(current_hunk)
            hunk_info = re.match(r"@@ -(\d+),(\d+) \+(\d+),(\d+) @@", line)
            if hunk_info:
                current_hunk = {
                    "old_start": int(hunk_info.group(1)),
                    "new_start": int(hunk_info.group(3)),
                    "lines": []
                }
        elif current_hunk and (line.startswith("+") or line.startswith("-") or line.startswith(" ")):
            current_hunk["lines"].append(line)
    if current_hunk:
        current_file["hunks"].append(current_hunk)
    if current_file:
        file_changes.append(current_file)
    return file_changes

def analyze_code_with_ai(diff_snippet):
    """使用 OpenAI 分析代码 diff"""
    prompt = f"""
    你是一名专业的代码审查者。请审阅以下代码 diff，提供具体的改进建议。
    关注代码质量、潜在 bug、性能问题和最佳实践。
    如适用，建议改进代码片段。
    Diff:
    ```diff
    {diff_snippet}
    ```
    返回格式化的反馈：
    - **Line [line_number]**: [反馈内容]
    - **建议** (可选): ```[language]\n[建议代码]\n```
    """
    response = client.chat.completions.create(
        model=OPENAI_MODEL,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=500
    )
    return response.choices[0].message.content

def post_comment(pr_number, repo, commit_id, file_path, line_number, comment, headers):
    """在 Pull Request 的指定 diff 处发表评论"""
    comment_url = f"https://api.github.com/repos/{repo}/pulls/{pr_number}/comments"
    body = {
        "body": comment,
        "commit_id": commit_id,
        "path": file_path,
        "line": line_number,
        "side": "RIGHT"
    }
    response = requests.post(comment_url, headers=headers, json=body)
    if response.status_code != 201:
        print(f"评论发布失败: {response.status_code}, {response.text}")

def main():
    with open(GITHUB_EVENT_PATH, "r") as f:
        event = json.load(f)
    pr_number = event["pull_request"]["number"]
    repo = event["repository"]["full_name"]
    commit_id = event["pull_request"]["head"]["sha"]

    headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json"
    }

    diff = get_pr_diff(pr_number, repo, headers)
    file_changes = parse_diff(diff)

    for file_change in file_changes:
        file_path = file_change["file"]
        for hunk in file_change["hunks"]:
            diff_snippet = "\n".join(hunk["lines"])
            feedback = analyze_code_with_ai(diff_snippet)
            for line in feedback.splitlines():
                if line.startswith("- **Line"):
                    line_number_match = re.match(r"- \*\*Line (\d+)\*\*: (.*)", line)
                    if line_number_match:
                        line_number = int(line_number_match.group(1)) + hunk["new_start"] - 1
                        comment = line_number_match.group(2)
                        post_comment(pr_number, repo, commit_id, file_path, line_number, comment, headers)

if __name__ == "__main__":
    main()