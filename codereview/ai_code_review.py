import requests
import re
import os
import json
import logging
from openai import OpenAI
from typing import Dict, List, Literal
from pydantic import BaseModel


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
client = OpenAI(api_key=OPENAI_API_KEY,
                base_url="https://api.allall.ai")


class CodeReviewIssue(BaseModel):
    type: Literal["bug", "security", "performance", "style", "best_practice"]
    severity: Literal["low", "medium", "high"]
    line: int
    suggestion: str
    code: str

# github api


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


def post_comment(pr_number, repo, commit_id, file_path, line_number, comment, headers, diff_hunk=None):
    """在 Pull Request 的指定 diff 处发表评论"""
    comment_url = f"https://api.github.com/repos/{repo}/pulls/{pr_number}/comments"

    # 确保行号是一个有效的整数
    try:
        line_number = int(line_number)
    except (ValueError, TypeError):
        logger.warning(
            f"Invalid line number: {line_number}, using default line 1")
        line_number = 1

    body = {
        "body": comment,
        "commit_id": commit_id,
        "path": file_path,
        "line": line_number,
        "side": "RIGHT"
    }

    # 如果提供了diff_hunk，添加到请求中
    if diff_hunk:
        body["diff_hunk"] = diff_hunk

    logger.info(
        f"Posting comment to {comment_url} for file {file_path} at line {line_number}")
    logger.debug(f"Comment body: {json.dumps(body)}")
    response = requests.post(comment_url, headers=headers, json=body)
    if response.status_code == 201:
        logger.info(
            f"Comment posted successfully, response code: {response.status_code}")
        return True
    else:
        logger.error(f"评论发布失败: {response.status_code}, {response.text}")
        logger.debug(f"Response headers: {response.headers}")

        # 如果失败，尝试获取更多错误信息
        try:
            error_info = response.json()
            logger.error(f"Error details: {json.dumps(error_info)}")
        except:
            pass

        return False

# llm api


def analyze_code_with_ai(formatted_change) -> List[CodeReviewIssue]:
    """使用 OpenAI 分析代码 diff"""

    prompt = f"""
    You are a senior Python code review expert, specializing in security, performance, readability, and best practices. Your task is to review the following code changes in a GitHub PR.

Provided diff information (grouped by file and hunk, including context lines, added lines, removed lines, and their line numbers):

{formatted_change}

Please conduct a code review based on the above diff. Focus on the following aspects:
- **Correctness**: Do the changes fix bugs or introduce new ones? Is the logic sound?
- **Security**: Are there any sensitive information leaks (e.g., API keys), file operation risks, or insufficient error handling?
- **Performance**: Are there inefficient loops, resource leaks, or unnecessary computations?
- **Readability & Style**: Does the code follow PEP 8? Are variable names clear? Are comments appropriate?
- **Best Practices**: Is appropriate exception handling used? Are new features fully implemented? Use of context managers?
- **Overall Impact**: What are the potential impacts of these changes on the project?

Output must be in strict JSON format only, do not have undeclared types, with no additional text. Use the following JSON structure, issues is an array, each item is a CodeReviewIssue object:
{{
        "issues":[{{
          "type": "bug|security|performance|style|best_practice (string)",
          "severity": "low|medium|high (string)",
          "line": "Comment start line (int)",
          "suggestion": "Fix suggestion (string)",
          "code": "Complete code line with fix (not just the changed part)",
        }}]
}}

If there are no issues, leave the array empty []. Ensure the JSON is valid.
    """
    logger.debug(f"Sending prompt to OpenAI with {len(prompt)} characters")
    response = client.chat.completions.create(
        model=OPENAI_MODEL,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=20000,
        response_format={"type": "json_object"},
    )
    
    # 解析JSON响应并转换为CodeReviewIssue对象
    try:
        response_text = response.choices[0].message.content
        logger.info(f"Response text: {response_text}")
        issues_data = json.loads(response_text)
        issues = [CodeReviewIssue(**issue) for issue in issues_data["issues"]]
        logger.debug(f"Received feedback with {len(issues)} issues")
        return issues
    except (json.JSONDecodeError, ValueError) as e:
        logger.error(f"Failed to parse AI response: {e}")
        logger.error(f"Raw response: {response.choices[0].message.content}")
        return []

# parse git diff


def parse_git_diff(diff_content: str) -> List[Dict]:
    """
    Parse a GitHub PR diff and extract filenames, line numbers, and code blocks.

    Args:
        diff_content (str): The raw diff content from a GitHub PR.

    Returns:
        List[Dict]: A list of dictionaries containing parsed diff information with filenames,
                    line numbers, and code blocks for added/modified lines.
    """
    diff_blocks = []
    current_file = None
    current_block = None
    lines = diff_content.splitlines()

    # Regex patterns
    file_pattern = re.compile(r'^diff --git a/(.+?) b/(.+?)$')
    hunk_pattern = re.compile(r'^@@ -(\d+,\d+) \+(\d+,\d+) @@')

    for line in lines:
        # Match file header
        file_match = file_pattern.match(line)
        if file_match:
            if current_file and current_block:
                diff_blocks.append(current_block)
            current_file = file_match.group(2)  # Use 'b/' path
            current_block = {
                'filename': current_file,
                'hunks': []
            }
            continue

        # Match hunk header
        hunk_match = hunk_pattern.match(line)
        if hunk_match and current_block:
            current_hunk = {
                'old_start': int(hunk_match.group(1).split(',')[0]),
                'new_start': int(hunk_match.group(2).split(',')[0]),
                'added_lines': [],
                'removed_lines': [],
                'context_lines': [],
                'old_line_num': int(hunk_match.group(1).split(',')[0]),  # 当前旧文件行号
                'new_line_num': int(hunk_match.group(2).split(',')[0])   # 当前新文件行号
            }
            current_block['hunks'].append(current_hunk)
            continue

        # Process code lines in a hunk
        if current_block and current_block['hunks']:
            current_hunk = current_block['hunks'][-1]
            if line.startswith('+') and not line.startswith('+++'):
                current_hunk['added_lines'].append({
                    'line_number': current_hunk['new_line_num'],
                    'content': line[1:].rstrip()
                })
                current_hunk['new_line_num'] += 1  # 只增加新文件行号
            elif line.startswith('-') and not line.startswith('---'):
                current_hunk['removed_lines'].append({
                    'line_number': current_hunk['old_line_num'],
                    'content': line[1:].rstrip()
                })
                current_hunk['old_line_num'] += 1  # 只增加旧文件行号
            elif line.startswith(' '):
                current_hunk['context_lines'].append({
                    'line_number': current_hunk['new_line_num'],
                    'content': line[1:].rstrip()
                })
                # context行在新旧文件中都存在，所以两个行号都要增加
                current_hunk['old_line_num'] += 1
                current_hunk['new_line_num'] += 1

    # Append the last block if it exists
    if current_block:
        diff_blocks.append(current_block)

    return diff_blocks

# format


def format_for_llm(diff_blocks: List[Dict]) -> List[str]:
    """
    Format parsed diff blocks into a list of strings, one per file, suitable for LLM code review.

    Args:
        diff_blocks (List[Dict]): Parsed diff blocks from parse_git_diff.

    Returns:
        List[str]: List of formatted strings, each containing diff information for one file.
    """
    file_outputs = []

    for block in diff_blocks:
        file_output = []
        file_output.append(f"File: {block['filename']}")

        for hunk in block['hunks']:
            file_output.append(
                f"\nHunk (new lines starting at {hunk['new_start']}):")
            if hunk['context_lines']:
                file_output.append("Context Lines:")
                for line in hunk['context_lines']:
                    file_output.append(
                        f"{line['line_number']}: {line['content']}")
            if hunk['removed_lines']:
                file_output.append("Removed Lines:")
                for line in hunk['removed_lines']:
                    file_output.append(
                        f"{line['line_number']} -: {line['content']}")
            if hunk['added_lines']:
                file_output.append("Added Lines:")
                for line in hunk['added_lines']:
                    file_output.append(
                        f"{line['line_number']} +: {line['content']}")

        # 将该文件的所有内容合并为一个字符串并添加到结果数组中
        file_outputs.append("\n".join(file_output))

    return file_outputs


def format_for_comment(issue: CodeReviewIssue) -> str:
    """
    Format code review issues into string with GitHub suggestion format.
    """
    issue_output = f"""
    Type: {issue.type}
    Severity: {issue.severity}
    suggestion: {issue.description}
    Code:
    ```suggestion
    {issue.code}
    ```
    """
    return issue_output


def main():
    logger.info("Starting code review process")
    with open(GITHUB_EVENT_PATH, "r") as f:
        event = json.load(f)
    pr_number = event["pull_request"]["number"]
    repo = event["repository"]["full_name"]
    commit_id = event["pull_request"]["head"]["sha"]
    logger.info(
        f"Processing PR #{pr_number} for repo {repo}, commit {commit_id}")

    headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json"
    }

    try:
        # 获取diff
        diff = get_pr_diff(pr_number, repo, headers)
        logger.info(
            f"Successfully fetched diff, length: {len(diff)} characters")
        # log diff
        logger.info(f"Diff: {diff}")
        # 解析diff文件
        file_changes = parse_git_diff(diff)
        formatted_changes = format_for_llm(file_changes)
        logger.info(f"File changes: {file_changes}")
    except Exception as e:
        logger.error(f"Error during diff processing: {str(e)}")
        return

    # 按文件提交
    for index, file_change in enumerate(file_changes):
        file_path = file_change["filename"]
        logger.info(f"Processing file: {file_path}")
        # 传递hunk信息给AI分析函数
        formatted_change = formatted_changes[index]
        issues = analyze_code_with_ai(formatted_change)
        logger.info(
             f"AI issues received total: {len(issues)} issues")

        comment_count = 0
        success_count = 0

        # 处理AI反馈
        for issue in issues:
                comment = format_for_comment(issue)
                success = post_comment(
                    pr_number, repo, commit_id, file_path, issue.line, comment, headers)
                comment_count += 1
                if success:
                    success_count += 1

        logger.info(
                f"Posted {success_count}/{comment_count} comments for file {file_path}")


if __name__ == "__main__":
    main()

