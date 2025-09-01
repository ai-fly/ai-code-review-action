import requests
import re
import os
import json
import logging
from openai import OpenAI

# 配置日志
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("ai_code_review")

# 从环境变量获取配置
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o")
GITHUB_EVENT_PATH = os.getenv("GITHUB_EVENT_PATH")

# 日志配置
DEBUG = os.getenv("DEBUG", "false").lower() == "true"
if not DEBUG:
    logger.setLevel(logging.INFO)

# 初始化 OpenAI 客户端
client = OpenAI(api_key=OPENAI_API_KEY, base_url="https://api.openai-prc.com/v1")

def get_pr_diff(pr_number, repo, headers):
    """获取 Pull Request 的 diff"""
    diff_url = f"https://api.github.com/repos/{repo}/pulls/{pr_number}"
    diff_headers = headers.copy()
    diff_headers["Accept"] = "application/vnd.github.diff"
    logger.info(f"Fetching PR diff from: {diff_url}")
    response = requests.get(diff_url, headers=diff_headers)
    logger.info(f"Diff API response status: {response.status_code}")
    if response.status_code == 200:
        return response.text
    else:
        logger.error(f"Diff API response content: {response.text[:200]}...")
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
                        "lines": [],
                        "header": line  # 保存完整的hunk头信息用于调试
                    }
                    logger.debug(f"Found hunk: {line} for file {file_path}")
        
        # 收集代码行
        elif current_hunk and current_file and (line.startswith("+") or line.startswith("-") or line.startswith(" ")):
            current_hunk["lines"].append(line)
    
    # 保存最后一个代码块和文件
    if current_hunk and current_file:
        current_file["hunks"].append(current_hunk)
    if current_file and current_file["hunks"]:
        file_changes.append(current_file)
    
    logger.info(f"Found {len(file_changes)} files with changes")
    for fc in file_changes:
        logger.info(f"File {fc['file']} has {len(fc['hunks'])} hunks")
        for i, hunk in enumerate(fc["hunks"]):
            logger.debug(f"Hunk {i+1}: {hunk['header']} with {len(hunk['lines'])} lines, new_start={hunk['new_start']}")
    
    return file_changes

def analyze_code_with_ai(diff_snippet, hunk_info=None):
    """使用 OpenAI 分析代码 diff"""
    hunk_desc = ""
    if hunk_info:
        hunk_desc = f"此代码块从第 {hunk_info['new_start']} 行开始。"
        logger.info(f"Analyzing hunk starting at line {hunk_info['new_start']} with {len(diff_snippet.splitlines())} lines")
    
    prompt = f"""
    你是一名专业的代码审查者。请审阅以下代码 diff，提供具体的改进建议。
    关注代码质量、潜在 bug、性能问题和最佳实践。
    如适用，建议改进代码片段。
    {hunk_desc}
    
    Diff:
    ```diff
    {diff_snippet}
    ```
    
    返回格式化的反馈：
    - **Line [line_number]**: [反馈内容]
    - **建议** (可选): ```[language]\n[建议代码]\n```
    
    注意：
    1. line_number 是相对于 diff 中显示的行号，而不是相对于文件开始的行号
    2. 请确保为所有新增（+开头）的代码行提供评论，特别是有潜在问题的代码
    3. 如果没有问题，也请至少提供一条改进建议
    """
    logger.debug(f"Sending prompt to OpenAI with {len(prompt)} characters")
    response = client.chat.completions.create(
        model=OPENAI_MODEL,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=1000  # 增加 token 限制以获取更详细的反馈
    )
    feedback = response.choices[0].message.content
    logger.debug(f"Received feedback with {len(feedback)} characters")
    logger.debug(f"Preview of feedback: {feedback[:100]}...")
    return feedback

def post_comment(pr_number, repo, commit_id, file_path, line_number, comment, headers):
    """在 Pull Request 的指定 diff 处发表评论"""
    comment_url = f"https://api.github.com/repos/{repo}/pulls/{pr_number}/comments"
    
    # 确保行号是一个有效的整数
    try:
        line_number = int(line_number)
    except (ValueError, TypeError):
        logger.warning(f"Invalid line number: {line_number}, using default line 1")
        line_number = 1
    
    body = {
        "body": comment,
        "commit_id": commit_id,
        "path": file_path,
        "line": line_number,
        "side": "RIGHT"
    }
    logger.info(f"Posting comment to {comment_url} for file {file_path} at line {line_number}")
    logger.debug(f"Comment body: {json.dumps(body)}")
    response = requests.post(comment_url, headers=headers, json=body)
    if response.status_code == 201:
        logger.info(f"Comment posted successfully, response code: {response.status_code}")
        return True
    else:
        logger.error(f"评论发布失败: {response.status_code}, {response.text}")
        logger.debug(f"Response headers: {response.headers}")
        return False

def main():
    logger.info("Starting code review process")
    with open(GITHUB_EVENT_PATH, "r") as f:
        event = json.load(f)
    pr_number = event["pull_request"]["number"]
    repo = event["repository"]["full_name"]
    commit_id = event["pull_request"]["head"]["sha"]
    logger.info(f"Processing PR #{pr_number} for repo {repo}, commit {commit_id}")

    headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json"
    }

    try:
        diff = get_pr_diff(pr_number, repo, headers)
        logger.info(f"Successfully fetched diff, length: {len(diff)} characters")
        logger.debug(f"Diff preview (first 10 lines):")
        diff_lines = diff.splitlines()
        for i in range(min(10, len(diff_lines))):
            logger.debug(f"  {diff_lines[i]}")
        file_changes = parse_diff(diff)
        logger.info(f"Parsed {len(file_changes)} changed files")
    except Exception as e:
        logger.error(f"Error during diff processing: {str(e)}")
        return

    for file_change in file_changes:
        file_path = file_change["file"]
        logger.info(f"Processing file: {file_path}")
        for hunk_index, hunk in enumerate(file_change["hunks"]):
            diff_snippet = "\n".join(hunk["lines"])
            logger.info(f"Analyzing hunk {hunk_index+1}/{len(file_change['hunks'])} starting at line {hunk['new_start']}")
            
            # 传递hunk信息给AI分析函数
            feedback = analyze_code_with_ai(diff_snippet, hunk_info=hunk)
            logger.info(f"AI feedback received, length: {len(feedback)} characters")
            
            comment_count = 0
            success_count = 0
            
            # 处理AI反馈
            for line in feedback.splitlines():
                if line.startswith("- **Line"):
                    line_number_match = re.match(r"- \*\*Line (\d+)\*\*: (.*)", line)
                    if line_number_match:
                        # 计算实际行号 - 相对行号 + 代码块起始行号 - 1
                        relative_line_number = int(line_number_match.group(1))
                        absolute_line_number = relative_line_number + hunk["new_start"] - 1
                        comment = line_number_match.group(2)
                        
                        logger.info(f"Posting comment at line {absolute_line_number} (relative line {relative_line_number})")
                        success = post_comment(pr_number, repo, commit_id, file_path, absolute_line_number, comment, headers)
                        comment_count += 1
                        if success:
                            success_count += 1
            
            logger.info(f"Posted {success_count}/{comment_count} comments for hunk {hunk_index+1}")
            
            # 如果没有评论，尝试为整个代码块添加一个通用评论
            if comment_count == 0:
                logger.info("No specific line comments found, adding a general comment for the hunk")
                general_comment = f"AI审查了从第{hunk['new_start']}行开始的代码块，但没有发现具体问题。请人工检查此代码块。"
                post_comment(pr_number, repo, commit_id, file_path, hunk["new_start"], general_comment, headers)

if __name__ == "__main__":
    main()