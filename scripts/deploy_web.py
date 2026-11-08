"""Deploy the dashboard against an existing pipeline; never deploy the legacy app."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def stack_values(stack):
    outputs = {item.get("Description", item["OutputKey"]): item["OutputValue"] for item in stack["Outputs"]}
    parameters = {item["ParameterKey"]: item["ParameterValue"] for item in stack["Parameters"]}
    return {"TableName": outputs["TABLE_NAME"], "OutputBucket": outputs["OUTPUT_BUCKET"],
            "ScannerFunction": outputs["SCANNER_FUNCTION"], "AlarmTopicArn": outputs["ALARM_TOPIC_ARN"],
            "PipelineStackArn": stack["StackId"], "CodeBucket": parameters["CodeBucket"]}


def frontend_config(outputs):
    return {"apiUrl": outputs["ApiUrl"], "clientId": outputs["UserPoolClientId"],
            "hostedUi": outputs["HostedUiDomain"], "redirectUri": outputs["SiteUrl"] + "/"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stack", nargs="?", default="podcast-lense-dev")
    parser.add_argument("region", nargs="?", default="us-east-1")
    args = parser.parse_args()
    if not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9-]{0,39}", args.stack):
        parser.error("Pipeline stack name must be at most 40 letters, digits or hyphens")

    def aws(*command, parse=True):
        result = subprocess.run(["aws", *command, "--region", args.region, "--no-cli-pager", "--output", "json"],
                                check=True, text=True, capture_output=True)
        return json.loads(result.stdout) if parse else result.stdout

    identity = aws("sts", "get-caller-identity")
    if identity["Arn"].endswith(":root"):
        raise ValueError("Use a non-root administrative user or role")
    stack = aws("cloudformation", "describe-stacks", "--stack-name", args.stack)["Stacks"][0]
    if stack["StackStatus"] not in {"CREATE_COMPLETE", "UPDATE_COMPLETE", "UPDATE_ROLLBACK_COMPLETE"}:
        raise ValueError("The pipeline stack must be in a stable completed state")
    parameters = stack_values(stack)
    web_stack = args.stack + "-web"
    print(f"Connecting {web_stack} to existing pipeline {args.stack} in {args.region}.", flush=True)
    print("Creates billable CloudFront, Cognito, API Gateway and Lambda resources. Existing pipeline data is reused.", flush=True)
    with tempfile.TemporaryDirectory(prefix="podcastlens-web-") as temporary:
        work = Path(temporary)
        venv, package = work / "venv", work / "package"
        subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True)
        subprocess.run([str(venv / "bin/python"), "-m", "pip", "install", "--quiet", "-r",
                        str(ROOT / "backend/requirements.txt"), "--target", str(package)], check=True)
        shutil.copytree(ROOT / "backend/src/podcastlens", package / "podcastlens",
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        archive = work / "api.zip"
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as target:
            for item in sorted(package.rglob("*")):
                if item.is_file() and "__pycache__" not in item.parts and item.suffix != ".pyc":
                    target.write(item, item.relative_to(package))
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        parameters["CodeKey"] = f"{args.stack}/web-api/{digest}.zip"
        aws("s3", "cp", str(archive), f"s3://{parameters['CodeBucket']}/{parameters['CodeKey']}", "--only-show-errors", parse=False)
        aws("cloudformation", "validate-template", "--template-body", "file://" + str(ROOT / "infrastructure/web.json"))
        print("Waiting for the dashboard stack; CloudFront can take several minutes.", flush=True)
        print(aws("cloudformation", "deploy", "--template-file", str(ROOT / "infrastructure/web.json"),
                  "--stack-name", web_stack, "--capabilities", "CAPABILITY_IAM", "--parameter-overrides",
                  *[f"{key}={value}" for key, value in parameters.items()], "--tags", "Project=PodcastLens",
                  "--no-fail-on-empty-changeset", parse=False), flush=True)
        result = aws("cloudformation", "describe-stacks", "--stack-name", web_stack)["Stacks"][0]
        outputs = {x["OutputKey"]: x["OutputValue"] for x in result["Outputs"]}
        site = work / "site"
        site.mkdir()
        for name in ("index.html", "styles.css", "app.js"):
            shutil.copyfile(ROOT / "web" / name, site / name)
        (site / "config.js").write_text("window.PODCASTLENS_CONFIG = " + json.dumps(frontend_config(outputs)) + ";\n")
        aws("s3", "sync", str(site), f"s3://{outputs['SiteBucketName']}/", "--delete", "--cache-control", "no-cache", parse=False)
        aws("cloudfront", "create-invalidation", "--distribution-id", outputs["DistributionId"], "--paths", "/*")
        print("Dashboard URL:", outputs["SiteUrl"], flush=True)
        print(f"Invite your operator login: python3 scripts/create_user.py YOUR_EMAIL {args.stack} {args.region}")


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as exc:
        print(exc.stderr or str(exc), file=sys.stderr)
        sys.exit(1)
    except (ValueError, KeyError) as exc:
        print(f"Deployment stopped: {exc}", file=sys.stderr)
        sys.exit(1)
