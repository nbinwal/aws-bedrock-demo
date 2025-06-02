# Documentation and Walkthrough of the Lambda Function

This Lambda function is designed to automate the response to CloudWatch alarms on EC2 instances. Whenever a CPU or Memory alarm transitions into the “ALARM” state, the function:

1. Gathers instance metadata (ID, tags, IPs, etc.).
2. Uses AWS Systems Manager (SSM) to identify the top resource‐consuming processes.
3. Sends a carefully crafted prompt to an AI model (Anthropic Claude‐v2 via Bedrock) requesting:

   * A human‐friendly troubleshooting paragraph (advice).
   * A JSON plan of corrective steps (SSM commands, plus justifications).
4. Executes any safe “kill process” or similar commands automatically on the EC2 instance, deferring any reboot command until after notifications are sent.
5. Immediately posts one single JSON‐formatted message (nine fields) to Slack, so on‐call engineers see exactly:

   * Which alarm fired, when, and why (CPU vs. Memory).
   * Which instance is affected and its key details.
   * The top 5 processes by CPU or memory.
   * The AI’s advice paragraph.
   * The exact SSM commands that were run (or if none were needed).
6. Publishes one single plaintext back to the same SNS topic (tagged as `source = "remediation"`) so that any email subscriber (e.g. Outlook) receives one complete email.
7. Prevents any infinite feedback loop by ignoring any SNS messages it has itself published (identified by `MessageAttributes.source == "remediation"`).

Below is a section‐by‐section explanation of the code and its logic.

---

## 1. Helper Function: `is_json(s: str) -> bool`

```python
def is_json(s: str) -> bool:
    """Return True if s is valid JSON."""
    try:
        json.loads(s)
        return True
    except:
        return False
```

* **Purpose**: Quickly checks if a given string can be parsed as JSON.
* **Why**: We need to differentiate between two “paths” of execution:

  * **JSON Path**: When SNS delivers a CloudWatch‐formatted JSON message.
  * **Plaintext Path**: When SNS re‐delivers our own plaintext (to send email) back to Lambda.
* **Outcome**: Returns `True` if the string is valid JSON; otherwise `False`.

---

## 2. Entry Point: `lambda_handler(event, context)`

This is the main function invoked by AWS Lambda whenever SNS publishes a message to this function’s subscription.

```python
record  = event['Records'][0]['Sns']
raw_msg = record.get('Message', "")
```

* We extract the first SNS record and get its raw message string.

```python
source_attr = record.get('MessageAttributes', {}) \
                    .get('source', {}) \
                    .get('StringValue')
if source_attr == "remediation":
    return {'statusCode': 200, 'body': 'skipped'}
```

* **Loop Prevention**: If the incoming SNS record has a message attribute `source = "remediation"`, it means this message is one we ourselves published after handling an alarm. We immediately return with a 200 status, doing nothing further. This avoids an infinite loop of Lambda → SNS → Lambda → SNS, etc.

---

## 3. JSON Path: Handling a New CloudWatch Alarm

```python
if is_json(raw_msg):
    sns_msg = json.loads(raw_msg)
```

* If `raw_msg` is valid JSON, we parse it into a Python dictionary (`sns_msg`).
* This branch only executes when CloudWatch actually sends its alarm payload (not our own plaintext).

### 3.1 Check Transition into ALARM

```python
old_state = sns_msg.get("OldStateValue", "")
new_state = sns_msg.get("NewStateValue", "")
if not (old_state != "ALARM" and new_state == "ALARM"):
    return {'statusCode': 200, 'body': 'skipped non-transition'}
```

* We inspect `OldStateValue` and `NewStateValue`:

  * Only proceed if **it was not already “ALARM”** and now it **just turned to “ALARM.”**
  * If the alarm was already in “ALARM” (or the state did not actually transition), we do nothing.

### 3.2 Extract Alarm Details

```python
alarm_name = sns_msg.get('AlarmName', 'UnknownAlarm')
timestamp  = sns_msg.get('StateChangeTime', 'UnknownTime')
```

* Retrieve the alarm’s name (e.g. `SmartOps-CPU-Alarm`) and the time of this state change.

```python
alarm_type = 'cpu'
if 'memory' in alarm_name.lower():
    alarm_type = 'memory'
```

* Determine whether this is a CPU alarm or a Memory alarm by checking if “memory” appears in the alarm name. Default is CPU.

### 3.3 Find the Affected EC2 Instance ID

```python
instance_id = None
trigger = sns_msg.get('Trigger', {}) or sns_msg.get('trigger', {})
for d in trigger.get('Dimensions', trigger.get('dimensions', [])):
    dim_name = d.get('Name') or d.get('name')
    if dim_name == 'InstanceId':
        instance_id = d.get('Value') or d.get('value')
        break
```

* CloudWatch SNS alarm messages include a `Trigger` block with `Dimensions`. One dimension’s `Name` is `InstanceId`. We loop through `Dimensions` to find that name and capture the associated `Value` (e.g. `i-04423b62c40d3f746`).

---

## 4. Section A: Fetch EC2 Metadata (`resource_details` + `metrics_url`)

```python
if instance_id:
    try:
        resp = ec2_client.describe_instances(InstanceIds=[instance_id])
        inst = resp['Reservations'][0]['Instances'][0]
    except:
        inst = {}
```

* If we successfully found an `instance_id`, we call `describe_instances` on the EC2 client to retrieve all details about that EC2 instance. If anything fails, we set an empty dictionary so subsequent dictionary lookups default to `'N/A'`.

```python
name_tag   = next((t['Value'] for t in inst.get('Tags', []) if t['Key']=='Name'), 'N/A')
inst_type  = inst.get('InstanceType', 'N/A')
state      = inst.get('State', {}).get('Name', 'N/A')
az         = inst.get('Placement', {}).get('AvailabilityZone', 'N/A')
launch_iso = inst.get('LaunchTime').isoformat() if inst.get('LaunchTime') else 'N/A'
priv_ip    = inst.get('PrivateIpAddress', 'N/A')
pub_ip     = inst.get('PublicIpAddress', 'N/A')
```

* We extract the following:

  * **Name** (from the “Name” tag, or “N/A” if missing).
  * **Instance Type** (e.g. `t2.small`).
  * **State** (e.g. `running` or `stopped`).
  * **Availability Zone** (e.g. `us-east-1b`).
  * **Launch Time** (ISO format string, or “N/A” if no `LaunchTime`).
  * **Private IP** and **Public IP**.

```python
resource_details = (
    f"Name: {name_tag}\n"
    f"InstanceId: {instance_id}\n"
    f"Type: {inst_type}\n"
    f"State: {state}\n"
    f"AZ: {az}\n"
    f"Launched: {launch_iso}\n"
    f"Private IP: {priv_ip}\n"
    f"Public IP: {pub_ip}"
)
```

* We build a multi‐line string named `resource_details` containing each of these values on its own line.

```python
metrics_url = (
    f"https://{AWS_REGION}.console.aws.amazon.com/cloudwatch/home"
    f"?region={AWS_REGION}#resource-health:dashboards/ec2/{instance_id}?~"
    f"(leadingMetric~'*22{alarm_type}-utilization*22)"
)
```

* We also craft a `metrics_url` that, when clicked, opens the AWS Console metrics dashboard for this EC2 instance (highlighting either CPU utilization or Memory utilization, depending on `alarm_type`).

```python
else:
    resource_details = "No EC2 instance ID found in alarm."
    metrics_url      = "N/A"
```

* If no `instance_id` was found in the alarm JSON, we fall back to placeholder text.

---

## 5. Section B: Fetch Top 5 Processes via SSM (`top_processes`, `top_process_name`)

```python
top_processes    = "Not available"
top_process_name = "Unknown"
```

* Default placeholders if SSM fails or no instance is found.

```python
if instance_id:
    try:
        if alarm_type == "cpu":
            shell_cmd = "ps -eo pid,comm,%cpu --sort=-%cpu | head -n6"
        else:
            shell_cmd = "ps -eo pid,comm,%mem --sort=-%mem | head -n6"
```

* We choose a shell command string based on the alarm type:

  * **CPU alarm**: list the top 5 CPU consumers (plus header line), sorted by `%CPU`.
  * **Memory alarm**: list the top 5 Memory consumers (plus header line), sorted by `%MEM`.

```python
cmd = ssm_client.send_command(
    InstanceIds=[instance_id],
    DocumentName="AWS-RunShellScript",
    Parameters={"commands": [shell_cmd]},
    TimeoutSeconds=30
)
cmd_id = cmd["Command"]["CommandId"]
```

* We dispatch that command to SSM’s `AWS-RunShellScript` Document on the target instance. We get back a `CommandId` which we will poll for completion.

```python
max_retries = 8
retry_delay = 2
inv = None
for attempt in range(max_retries):
    try:
        inv = ssm_client.get_command_invocation(
            CommandId=cmd_id,
            InstanceId=instance_id
        )
        if inv["Status"] in ("Success", "Failed", "TimedOut", "Cancelled"):
            break
    except ClientError as error:
        if error.response['Error']['Code'] == 'InvocationDoesNotExist':
            time.sleep(retry_delay)
            continue
        else:
            raise
    time.sleep(retry_delay)
```

* We poll up to 8 times, waiting 2 seconds between attempts, until the SSM command’s status is one of `Success`, `Failed`, `TimedOut`, or `Cancelled`. We handle the occasional `InvocationDoesNotExist` error by waiting briefly and retrying.

```python
if inv and inv["Status"] == "Success":
    top_processes = inv["StandardOutputContent"].strip()
    lines = top_processes.splitlines()
    if len(lines) >= 2:
        first_proc_line = lines[1].strip()
        parts = first_proc_line.split()
        if len(parts) >= 2:
            top_process_name = parts[1]
elif inv:
    top_processes = f"SSM command status: {inv['Status']}"
else:
    top_processes = "SSM command did not complete"
```

* If the command succeeded, we:

  1. Grab the raw STDOUT text in `top_processes`. It looks like:

     ```
     PID COMMAND         %CPU
     1234 java           75.0
     5678 python         13.5
     …  
     ```
  2. Split into lines and parse the second line (`lines[1]`) to isolate the process name of the very top consumer (e.g. `java`). That becomes `top_process_name`, used only if AI’s plan is missing or generic.
* If the SSM command ran but did not succeed, we note that status text.
* If there was no invocation at all, we mark that as a failure.

```python
except Exception as e:
    top_processes = f"Error fetching processes via SSM: {e}"
```

* Any unexpected exception yields a human‐readable error string in place of `top_processes`.

---

## 6. Section C: Ask Bedrock (Claude-v2) for Advice + JSON Plan

```python
metric_unit = "%CPU" if alarm_type == "cpu" else "%MEM"
prompt = (
    f"Human: A CloudWatch alarm '{alarm_name}' for EC2 instance {instance_id or 'Unknown'} "
    f"fired at {timestamp} due to high {alarm_type.upper()} usage.\n\n"
    f"The top process consuming {metric_unit} is: '{top_process_name}'.\n"
    f"Here are the top 5 processes by {metric_unit}:\n{top_processes}\n\n"

    "###ADVICE###\n"
    "Please provide a clear, concise paragraph of human-friendly advice "
    "for a cloud engineer explaining what to check and how to fix this.\n\n"

    "###PLAN###\n"
    "Now output a JSON object with:\n"
    "  \"actions\": [ { \"type\": \"ssm_command\", \"description\": \"…\", \"commands\": [\"…\"] }, … ],\n"
    "  \"justifications\": [ \"…\", \"…\" ]\n"
    "Do not output anything except those two sections, separated by the markers above.\n"
    "Assistant:"
)
```

* We build a single prompt string that contains:

  1. A brief summary of the alarm event (instance, time, CPU vs Memory).
  2. The raw “top 5 processes” text from SSM.
  3. The marker `###ADVICE###` followed by a request for a single paragraph of plain‐English troubleshooting advice.
  4. The marker `###PLAN###` followed by a request to output a JSON object containing:

     * An `"actions"` array of objects, each with:

       * `"type": "ssm_command"`
       * `"description": "…"`
       * `"commands": ["…"]`
     * A parallel `"justifications"` array of strings explaining why each action is recommended.
  5. A final instruction: “Do not output anything except those two sections.”

```python
try:
    resp = bedrock_client.invoke_model(
        modelId=MODEL_ID,
        contentType="application/json",
        accept="application/json",
        body=json.dumps({
            "prompt": prompt,
            "max_tokens_to_sample": 400,
            "temperature": 0.5
        })
    )
    raw = resp["body"].read().decode('utf-8')
except Exception:
    raw = "###ADVICE###\nBedrock unavailable; defaulting to kill top process.\n\n###PLAN###\n{}"
```

* We send that prompt to Bedrock’s `anthropic.claude-v2` model.
* If Bedrock is unreachable or errors out, we use a fallback:

  * Advice: “Bedrock unavailable; defaulting to kill top process.”
  * Plan: empty JSON object (which triggers our fallback logic later).

```python
# C1) If Bedrock returned {"type":"completion","completion":…}, extract it
try:
    maybe_obj = json.loads(raw)
    if isinstance(maybe_obj, dict) and "completion" in maybe_obj:
        raw = maybe_obj["completion"]
except:
    pass
```

* Sometimes Bedrock returns a JSON envelope like:

  ```json
  {"type":"completion","completion":"<actual text including ###ADVICE### and ###PLAN###>"}
  ```
* We detect that envelope and extract the `"completion"` field so that `raw` holds exactly the AI’s content (the two marked sections).

---

## 7. Section D: Split AI Output into `advice_main` and `plan_obj`

```python
advice_main = ""
plan_obj    = {}
if "###ADVICE###" in raw and "###PLAN###" in raw:
    parts = raw.split("###PLAN###", 1)
    advice_section = parts[0].replace("###ADVICE###", "").strip()
    plan_section   = parts[1].strip()
    advice_main    = advice_section
    try:
        plan_obj = json.loads(plan_section)
    except:
        plan_obj = {}
else:
    advice_main = raw
    plan_obj    = {}
```

* If the AI’s text contains both markers (`###ADVICE###` and `###PLAN###`):

  1. We split on `###PLAN###` to isolate the advice vs. the JSON plan.
  2. We remove `###ADVICE###` from the first half and store that as `advice_main` (human‐friendly paragraph).
  3. We parse the second half as JSON, storing the result in `plan_obj`. If parsing fails, we use an empty dictionary.
* If the markers are missing entirely, we treat the entire AI response as `advice_main` and leave `plan_obj` empty.

---

## 8. Section E: Fallback if AI Provided No Actions

```python
if not plan_obj.get("actions"):
    if top_process_name and top_process_name != "Unknown":
        plan_obj["actions"] = [{
            "type":        "ssm_command",
            "description": f"Kill top process '{top_process_name}' to reduce load",
            "commands":    [f"sudo pkill -9 {top_process_name}"]
        }]
        plan_obj["justifications"] = [
            f"Killing '{top_process_name}' will immediately reduce CPU/Memory usage."
        ]
    else:
        plan_obj["actions"]        = []
        plan_obj["justifications"] = []
```

* If `plan_obj["actions"]` is missing or empty, we create a single fallback action:

  * **Type**: `ssm_command`
  * **Description**: “Kill top process `<name>` to reduce load.”
  * **Commands**: A list containing `sudo pkill -9 <name>`.
  * We also add a justification explaining why we are killing that process.
* If we could not identify any top process (e.g. `top_process_name == "Unknown"`), we leave both arrays empty.

---

## 9. Section F: Execute SSM Actions, Deferring Any Reboot

```python
executed_actions = []
justifications   = plan_obj.get("justifications", [])
deferred_reboot  = None

for idx, action in enumerate(plan_obj.get("actions", [])):
    if action.get("type") == "ssm_command" and instance_id:
        cmd_list      = action.get("commands", [])
        description   = action.get("description", "")
        justification = justifications[idx] if idx < len(justifications) else ""
```

* We prepare:

  * An empty list `executed_actions` to record each SSM command we run.
  * A reference list `justifications` so we can attach the correct justification to each action.
  * A variable `deferred_reboot = None` to capture any “reboot” steps without executing them immediately.

```python
# Defer any “reboot” command
if any("reboot" in c.lower() for c in cmd_list):
    deferred_reboot = {
        "description":   description,
        "commands":      cmd_list,
        "justification": justification
    }
    continue
```

* If any command in `cmd_list` contains the substring `"reboot"` (case‐insensitive), we push the entire action object into `deferred_reboot` and skip running it for now.
* This ensures we do not accidentally reboot the instance before we send Slack/email.

```python
# Normalize “kill <stress_pid>” or “grep stress” → “sudo pkill -9 stress”
final_cmds = []
for c in cmd_list:
    if "<stress_pid>" in c or "grep stress" in c:
        final_cmds = [f"sudo pkill -9 {top_process_name}"]
        break
if not final_cmds:
    final_cmds = cmd_list
```

* If AI wrote a generic placeholder command like `kill <stress_pid>` or “grep stress,” we replace it with a concrete `sudo pkill -9 <top_process_name>`. Otherwise, we use `cmd_list` as‐is.

```python
try:
    ssm_resp = ssm_client.send_command(
        InstanceIds=[instance_id],
        DocumentName="AWS-RunShellScript",
        Parameters={"commands": final_cmds},
        TimeoutSeconds=60
    )
    cmd_id = ssm_resp["Command"]["CommandId"]
    executed_actions.append({
        "description":   description,
        "commands":      final_cmds,
        "command_id":    cmd_id,
        "status":        "SENT",
        "justification": justification
    })
except ClientError as e:
    executed_actions.append({
        "description":   description,
        "commands":      final_cmds,
        "status":        f"FAILED: {str(e)}",
        "justification": justification
    })
```

* We send the final commands via SSM. If SSM accepts them, we record:

  * `description`
  * the actual `commands` array we sent
  * the returned `command_id`
  * `status = "SENT"`
  * `justification`
* If SSM throws an error (e.g. instance offline, IAM missing), we record the failure with the error message in `status`.

---

## 10. Section G: Scrub AWS Documentation URLs from Advice

```python
urls       = re.findall(r'https?://(?:docs\.aws\.amazon\.com|aws\.amazon\.com)/\S+', advice_main)
valid_urls = []
for u in urls:
    try:
        r = http.request("HEAD", u, timeout=5.0)
        if 200 <= r.status < 400:
            valid_urls.append(u)
    except:
        pass
if valid_urls:
    advice_main = advice_main.split(valid_urls[0])[0].strip()
    advice_main += "\n\nAWS Documentation Links:\n" + "\n".join(valid_urls)
```

* We search the advice paragraph for any links to AWS documentation (`docs.aws.amazon.com` or `aws.amazon.com`).
* For each URL found, we send an HTTP `HEAD` request to confirm the link is still live (status code 200–399).
* We collect any “valid” links into `valid_urls`. If we found at least one valid link:

  1. We remove everything from the first link onward in `advice_main` (so that the paragraph remains concise).
  2. We append a new section at the bottom of `advice_main` listing all the `valid_urls` under “AWS Documentation Links:”.

This keeps the advice paragraph focused on troubleshooting steps, while still preserving clickable reference links at the end.

---

## 11. Section H: Send One Single Slack Notification (Nine Fields)

```python
if SLACK_WEBHOOK_URL:
    if executed_actions:
        actions_taken_lines = []
        for x in executed_actions:
            actions_taken_lines.append(
                f"- Description: {x['description']}\n"
                f"  Commands: {x['commands']}\n"
                f"  Status: {x['status']}\n"
                f"  Justification: {x.get('justification','')}"
            )
        if deferred_reboot:
            actions_taken_lines.append(
                f"- Description: {deferred_reboot['description']}\n"
                f"  Commands: {deferred_reboot['commands']}\n"
                "  Status: PENDING REBOOT\n"
                f"  Justification: {deferred_reboot['justification']}"
            )
        actions_taken_str = "\n\n".join(actions_taken_lines)
    else:
        actions_taken_str = "No automated actions were taken."

    slack_payload = {
        "alarm_name":       alarm_name,
        "timestamp":        timestamp,
        "resource_details": resource_details,
        "metrics_type":     alarm_type.upper(),
        "metrics_url":      metrics_url,
        "processes_type":   alarm_type.upper(),
        "top_processes":    top_processes,
        "advice":           advice_main,
        "actions_taken":    actions_taken_str
    }

    max_retries = 2
    for attempt in range(max_retries + 1):
        try:
            response = http.request(
                "POST",
                SLACK_WEBHOOK_URL,
                body=json.dumps(slack_payload).encode("utf-8"),
                headers={"Content-Type": "application/json"}
            )
            if response.status == 200:
                break
        except:
            time.sleep(2 ** attempt)
```

* We only send Slack after all SSM commands have been attempted, so that `actions_taken` is final.
* We build a multi‐line string `actions_taken_str`. For each entry in `executed_actions`, we add:

  ```
  - Description: <…>
    Commands: <…>
    Status: <…>
    Justification: <…>
  ```

  on separate lines. If there is a deferred reboot, we append that as well with `Status: PENDING REBOOT`.
* If no actions were executed, we set `actions_taken_str = "No automated actions were taken."`
* Then we create a JSON object `slack_payload` with exactly these nine keys:

  1. **alarm\_name**: The name of the alarm that fired.
  2. **timestamp**: The time the alarm went to “ALARM.”
  3. **resource\_details**: Multi‐line string of instance metadata.
  4. **metrics\_type**: `"CPU"` or `"MEMORY"` (uppercase).
  5. **metrics\_url**: The CloudWatch metrics console link.
  6. **processes\_type**: `"CPU"` or `"MEMORY"` (same as `metrics_type`).
  7. **top\_processes**: The raw text output (top 5 lines) from the SSM “ps” command.
  8. **advice**: The AI‐generated troubleshooting paragraph (with documentation links appended).
  9. **actions\_taken**: The multi‐line description of every SSM command that was sent (or “No automated actions were taken.”).
* We issue an HTTP POST to the Slack Incoming Webhook (`SLACK_WEBHOOK_URL`), converting our object to JSON and setting `Content-Type: application/json`. We retry up to 2 times (with exponential backoff) if the first attempt fails.

---

## 12. Section I: Publish One Single Plaintext to SNS (for Outlook/Email)

```python
subject         = f"[Alert] EC2 Alarm: {alarm_name}"
metrics_label   = f"{alarm_type.upper()} Metrics Console"
processes_label = f"Top 5 {alarm_type.upper()} Processes"

executed_summary = "\n\n=== Actions Taken by AI ===\n"
for x in executed_actions:
    executed_summary += (
        f"- Description: {x['description']}\n"
        f"  Commands: {x['commands']}\n"
        f"  Status: {x['status']}\n"
        f"  Justification: {x.get('justification','')}\n\n"
    )
if deferred_reboot:
    executed_summary += (
        f"- Description: {deferred_reboot['description']}\n"
        f"  Commands: {deferred_reboot['commands']}\n"
        "  Status: PENDING REBOOT\n"
        f"  Justification: {deferred_reboot['justification']}\n\n"
    )
elif not executed_actions:
    executed_summary += "No automated actions were taken.\n"
```

* We prepare a human‐readable summary string `executed_summary` that will show up in the email. It begins with:

  ```
  === Actions Taken by AI ===
  - Description: <…>
    Commands: <…>
    Status: <…>
    Justification: <…>
  ```

  for every executed action. We also add any deferred reboot entry.

```python
message_body = (
    f"Alarm: {alarm_name}\n"
    f"Time: {timestamp}\n\n"
    f"Resource Details:\n{resource_details}\n\n"
    f"{metrics_label}:\n{metrics_url}\n\n"
    f"{processes_label}:\n{top_processes}\n\n"
    f"General Remediation Advice:\n{advice_main}"
    f"{executed_summary}"
)
```

* We build a plaintext `message_body` exactly in the format that Outlook (or any email) can consume. It contains:

  1. **Alarm** and **Time** at the top (two lines).
  2. Blank line, then **“Resource Details:”** followed by the multi‐line `resource_details` block.
  3. Blank line, **“CPU Metrics Console:”** (or “Memory Metrics Console:”) and the `metrics_url` on its own line.
  4. Blank line, **“Top 5 CPU Processes:”** (or “Top 5 Memory Processes:”) and the raw `top_processes` text.
  5. Blank line, **“General Remediation Advice:”** and the AI’s advice paragraph.
  6. Immediately after, the multi‐line `executed_summary` (showing which SSM commands were run and their status).
* Because we send this **once** (after Slack), subscribers on that SNS topic will each receive exactly one email containing the entire context.

```python
sns_client.publish(
    TopicArn=SNS_TOPIC_ARN,
    Subject=subject,
    Message=message_body,
    MessageAttributes={
        "source": {"DataType":"String", "StringValue":"remediation"},
        "slack":  {"DataType":"String", "StringValue":"true"}
    }
)
```

* We publish `message_body` back to SNS with two attributes:

  1. `"source" = "remediation"`: Ensures that if this plaintext re‐invokes Lambda, we skip it (loop prevention).
  2. `"slack" = "true"` (optional marker).
* Because we attached `source="remediation"`, the next invocation from this same plaintext SNS message will see that attribute, immediately return, and never send Slack/email again.

```python
return {'statusCode': 200, 'body': json.dumps({'status': 'ok'})}
```

* Finally, we return an HTTP 200 so Lambda knows our function executed successfully.

---

## 13. Plaintext Path: Skip Slack (When SNS Re‐Invokes with “Alarm:” Text)

```python
if raw_msg.startswith("Alarm:"):
    # We used to send Slack here again; now we skip Slack.
    # Outlook already got this plaintext as an email.
    return {'statusCode': 200, 'body': 'skipped plaintext path (no Slack)'}
```

* If SNS has re‐sent our own plaintext (which always begins with “Alarm: \<alarm\_name>”), we simply return with a 200 and do nothing.
* **Why**: We’ve already delivered the email once. We do not want to send Slack again or re‐execute the remediation steps. This prevents a “double post” to Slack or any repeated action.

---

## 14. Unknown Format Path

```python
return {'statusCode': 200, 'body': 'skipped unknown format'}
```

* If the SNS message is neither valid JSON nor plaintext starting with “Alarm:”, we assume it’s some unrelated notification. We do nothing and return a 200 status.

---

## 15. In Summary: End‐to‐End Flow

1. **CloudWatch Alarm → SNS JSON**

   * Lambda checks `OldStateValue` → `NewStateValue`: only proceed if the alarm just became “ALARM.”
   * Gather EC2 metadata and top processes via SSM.
   * Ask Bedrock AI for advice (paragraph) and plan (JSON).
   * Execute any safe SSM actions immediately, deferring reboots.
   * Scrub out AWS doc links, append at the bottom of advice.
   * **Send one Slack message** with exactly nine fields:

     1. `alarm_name`
     2. `timestamp`
     3. `resource_details`
     4. `metrics_type`
     5. `metrics_url`
     6. `processes_type`
     7. `top_processes`
     8. `advice`
     9. `actions_taken`
   * **Publish one plaintext** back to SNS (same topic) with `source="remediation"` so email subscribers receive a single, complete email.

2. **SNS “Plaintext” → Lambda**

   * Raw message starts with `"Alarm:"`. This is exactly our own plaintext.
   * Lambda sees it, does **not** send Slack or run any steps.
   * Return 200.

3. **Loop Prevention**

   * The moment we publish back to SNS in step (1), that same plaintext would normally trigger Lambda again.
   * We avoid doing anything on that invocation because:

     * We added `MessageAttributes["source"] = "remediation"`.
     * At the top of Lambda, we check if `source_attr == "remediation"`. If so, we immediately return “skipped.”
   * This ensures a single pass for each alarm transition: one Slack post + one email, and nothing more.

---

## 16. Why This Design Matters (Layman’s Perspective)

* **Single Notification, No Spam**

  * Engineers on Slack get one concise JSON containing everything they need.
  * Ops teams on email get one single plaintext email.
  * No partial updates, no repeated messages, no infinite loops.

* **Automated Diagnosis & Remediation**

  * The function checks “ps” on the instance for you.
  * It submits your data to an AI model that generates a plain‐English explanation and a set of commands.
  * It runs those commands instantly (e.g. “kill stress”) so you do not have to log in or type anything manually.

* **Deferred Reboot**

  * If AI thinks you should reboot, that command is deferred until notifications are sent.
  * This way, you get to read the Slack message or email before the instance reboots.

* **Clean Separation of Concerns**

  1. **CloudWatch Alarm JSON** → triggers all logic, Slack + email.
  2. **Plaintext message** (our own follow‐up) → triggers only “do nothing.”
  3. **No reliance on manual intervention** once set up.

* **Built‐In Transparency**

  * Slack shows the raw “ps” output, so you know exactly which processes were high.
  * The AI’s “advice” paragraph is human‐readable and immediately actionable.
  * The “actions\_taken” section lists exactly which SSM commands were sent, with justifications.

By reading this documentation, anyone (developer, DevOps engineer, or manager) can understand:

* **Why** this Lambda exists (to automatically handle CPU/Memory alarms on EC2).
* **How** it processes a new alarm end‐to‐end.
* **What** each section of the code is responsible for.
* **How** Slack and email notifications are triggered exactly once per alarm.
* **How** any automatic remediation commands are chosen, normalized, and executed.

In short, this Lambda acts as an AI‐powered “smart ops copilot” for EC2 alarms, doing diagnosis, advice, remediation, and notifications in a single, coherent workflow.
