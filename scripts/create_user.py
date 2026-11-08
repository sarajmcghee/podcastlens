"""Invite one trusted operator to the connected dashboard."""
import argparse
import json
import subprocess


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("email")
    parser.add_argument("stack", nargs="?", default="podcast-lense-dev")
    parser.add_argument("region", nargs="?", default="us-east-1")
    args = parser.parse_args()
    if "@" not in args.email or any(c.isspace() for c in args.email):
        parser.error("Provide the operator's email address")
    def aws(*command):
        result = subprocess.run(["aws", *command, "--region", args.region, "--output", "json", "--no-cli-pager"],
                                check=True, capture_output=True, text=True)
        return json.loads(result.stdout or "{}")
    stack = aws("cloudformation", "describe-stacks", "--stack-name", args.stack + "-web")["Stacks"][0]
    pool = next(x["OutputValue"] for x in stack["Outputs"] if x["OutputKey"] == "UserPoolId")
    try:
        user = aws("cognito-idp", "admin-get-user", "--user-pool-id", pool, "--username", args.email)
        username = user["Username"]
    except subprocess.CalledProcessError as exc:
        if "UserNotFoundException" not in (exc.stderr or ""):
            raise
        user = aws("cognito-idp", "admin-create-user", "--user-pool-id", pool, "--username", args.email,
                   "--user-attributes", json.dumps([{"Name":"email","Value":args.email},{"Name":"email_verified","Value":"true"}]),
                   "--desired-delivery-mediums", "EMAIL")
        username = user["User"]["Username"]
    aws("cognito-idp", "admin-add-user-to-group", "--user-pool-id", pool, "--username", username, "--group-name", "operators")
    print("Operator access configured. New users receive an email invitation to set a password.")


if __name__ == "__main__":
    main()
