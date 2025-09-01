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
    diff_headers = headers.copy()
    diff_headers["Accept"] = "application/vnd.github.diff"
    print(f"Fetching PR diff from: {diff_url}")
    response = requests.get(diff_url, headers=diff_headers)
    print(f"Diff API response status: {response.status_code}")
    if response.status_code == 200:
        return response.text
    else:
        print(f"Diff API response content: {response.text[:200]}...")
        raise Exception(f"Failed to fetch diff: {response.status_code}")

def parse_diff(diff):
    """解析 diff，提取文件、行号和代码块"""
    diff_lines = diff.splitlines()
    file_changes = []
    current_file = None
    current_hunk = None
    file_path = None
    
    for line in diff_lines:
        # 检测新文件的开始
        if line.startswith("diff --git"):
            # 保存前一个文件的信息
            if current_file and current_file["hunks"]:
                file_changes.append(current_file)
            
            # 重置当前文件信息
            file_path = None
            current_file = None
            current_hunk = None
            
        # 提取文件路径
        elif line.startswith("--- a/") or line.startswith("+++ b/"):
            path = line[6:]  # 跳过 "--- a/" 或 "+++ b/"
            if line.startswith("+++ b/") and path != "/dev/null":
                file_path = path
                current_file = {"file": file_path, "hunks": []}
                
        # 解析代码块信息
        elif line.startswith("@@"):
            if current_file:  # 确保我们有一个有效的文件
                if current_hunk:
                    current_file["hunks"].append(current_hunk)
                
                # 匹配 "@@ -71,7 +71,6 @@" 格式
                hunk_info = re.match(r"@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@", line)
                if hunk_info:
                    current_hunk = {
                        "old_start": int(hunk_info.group(1)),
                        "new_start": int(hunk_info.group(2)),
                        "lines": []
                    }
        
        # 收集代码行
        elif current_hunk and current_file and (line.startswith("+") or line.startswith("-") or line.startswith(" ")):
            current_hunk["lines"].append(line)
    
    # 保存最后一个代码块和文件
    if current_hunk and current_file:
        current_file["hunks"].append(current_hunk)
    if current_file and current_file["hunks"]:
        file_changes.append(current_file)
    
    print(f"Debug: Found {len(file_changes)} files with changes")
    for fc in file_changes:
        print(f"Debug: File {fc['file']} has {len(fc['hunks'])} hunks")
    
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
    print(f"Posting comment to {comment_url}")
    print(f"Comment body: {json.dumps(body)}")
    response = requests.post(comment_url, headers=headers, json=body)
    if response.status_code == 201:
        print(f"Comment posted successfully, response code: {response.status_code}")
    else:
        print(f"评论发布失败: {response.status_code}, {response.text}")

def main():
    print("Starting code review process")
    with open(GITHUB_EVENT_PATH, "r") as f:
        event = json.load(f)
    pr_number = event["pull_request"]["number"]
    repo = event["repository"]["full_name"]
    commit_id = event["pull_request"]["head"]["sha"]
    print(f"Processing PR #{pr_number} for repo {repo}, commit {commit_id}")

    headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json"
    }

    try:
        diff = get_pr_diff(pr_number, repo, headers)
        print(f"Successfully fetched diff, length: {len(diff)} characters")
        print(f"Diff preview (first 10 lines):")
        diff_lines = diff.splitlines()
        for i in range(min(10, len(diff_lines))):
            print(f"  {diff_lines[i]}")
        file_changes = parse_diff(diff)
        print(f"Parsed {len(file_changes)} changed files")
    except Exception as e:
        print(f"Error during diff processing: {str(e)}")
        return

    for file_change in file_changes:
        file_path = file_change["file"]
        print(f"Processing file: {file_path}")
        for hunk in file_change["hunks"]:
            diff_snippet = "\n".join(hunk["lines"])
            print(f"  Analyzing hunk starting at line {hunk['new_start']}")
            feedback = analyze_code_with_ai(diff_snippet)
            print(f"  AI feedback received, length: {len(feedback)} characters")
            comment_count = 0
            for line in feedback.splitlines():
                if line.startswith("- **Line"):
                    line_number_match = re.match(r"- \*\*Line (\d+)\*\*: (.*)", line)
                    if line_number_match:
                        line_number = int(line_number_match.group(1)) + hunk["new_start"] - 1
                        comment = line_number_match.group(2)
                        print(f"  Posting comment at line {line_number}")
                        post_comment(pr_number, repo, commit_id, file_path, line_number, comment, headers)
                        comment_count += 1
            print(f"  Posted {comment_count} comments for this hunk")

if __name__ == "__main__":
    main()