"""Contract checks for attaching the dashboard to existing stack outputs."""
import json
import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.deploy_web import frontend_config, stack_values


class DeploymentContract(unittest.TestCase):
    def test_dashboard_requires_invited_users_and_authenticator_mfa(self):
        template = json.loads((Path(__file__).resolve().parents[1] / "infrastructure/web.json").read_text())
        resources = template["Resources"]
        pool = resources["UserPool"]["Properties"]
        self.assertTrue(pool["AdminCreateUserConfig"]["AllowAdminCreateUserOnly"])
        self.assertEqual(pool["MfaConfiguration"], "ON")
        self.assertEqual(pool["EnabledMfas"], ["SOFTWARE_TOKEN_MFA"])
        self.assertNotIn("SmsConfiguration", pool)
        for resource in resources.values():
            if resource["Type"] == "AWS::ApiGatewayV2::Route":
                with self.subTest(route=resource["Properties"]["RouteKey"]):
                    self.assertEqual(resource["Properties"]["AuthorizationType"], "JWT")
                    self.assertIn("podcastlens/access", resource["Properties"]["AuthorizationScopes"])

    def test_pipeline_outputs_supply_existing_resources(self):
        names = {"TABLE_NAME":"existing-table", "OUTPUT_BUCKET":"existing-transcripts",
                 "SCANNER_FUNCTION":"existing-scanner", "ALARM_TOPIC_ARN":"existing-topic"}
        stack = {"StackId":"existing-stack-arn", "Parameters":[{"ParameterKey":"CodeBucket","ParameterValue":"existing-artifacts"}],
                 "Outputs":[{"OutputKey":key.replace('_',''),"Description":key,"OutputValue":value} for key,value in names.items()]}
        self.assertEqual(stack_values(stack), {"TableName":"existing-table","OutputBucket":"existing-transcripts",
            "ScannerFunction":"existing-scanner","AlarmTopicArn":"existing-topic","PipelineStackArn":"existing-stack-arn",
            "CodeBucket":"existing-artifacts"})

    def test_site_config_uses_new_api_and_exact_callback(self):
        self.assertEqual(frontend_config({"ApiUrl":"https://api.example","UserPoolClientId":"client",
            "HostedUiDomain":"https://login.example","SiteUrl":"https://site.example"}),
            {"apiUrl":"https://api.example","clientId":"client","hostedUi":"https://login.example","redirectUri":"https://site.example/"})

    def test_unrelated_stack_is_rejected_before_deployment(self):
        with self.assertRaises(KeyError):
            stack_values({"StackId":"other","Parameters":[],"Outputs":[]})


if __name__ == "__main__":
    unittest.main(verbosity=2)
