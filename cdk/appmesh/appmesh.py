import os
from constructs import Construct
from aws_cdk import (
    CfnOutput,
    aws_appmesh,
    aws_ec2,
    aws_ecs as ecs,
    aws_iam,
    aws_logs as logs,
    aws_ecs_patterns as ecs_patterns,
)


class AppMesh(Construct):

    def __init__(self, scope: Construct, id: str, *, ecs_cluster, xray=False):

        super().__init__(scope, id)

        mesh = aws_appmesh.Mesh(self,"EcsWorkShop-AppMesh", mesh_name="ecs-mesh")

        # We will create a App Mesh Virtual Gateway
        mesh_vgw = aws_appmesh.VirtualGateway(
            self,
            "Mesh-VGW",
            mesh=mesh,
            listeners=[aws_appmesh.VirtualGatewayListener.http(
                port=3000
                )],
            virtual_gateway_name="ecsworkshop-vgw"
        )

        # Creating the mesh gateway task for the frontend app
        # For more info related to App Mesh Proxy check https://docs.aws.amazon.com/app-mesh/latest/userguide/getting-started-ecs.html
        mesh_gw_proxy_task_def = ecs.FargateTaskDefinition(
            self,
            "mesh-gw-proxy-taskdef",
            cpu=256,
            memory_limit_mib=512,
            family="mesh-gw-proxy-taskdef",
        )

        # LogGroup for the App Mesh Proxy Task
        logGroup = logs.LogGroup(self,"ecsworkshopMeshGateway",
            #log_group_name="ecsworkshop-mesh-gateway",
            retention=logs.RetentionDays.ONE_WEEK
        )

        # App Mesh Virtual Gateway Envoy proxy Task definition
        # For a use specific ECR region, please check https://docs.aws.amazon.com/app-mesh/latest/userguide/envoy.html
        container = mesh_gw_proxy_task_def.add_container(
            "mesh-gw-proxy-contdef",
            image=ecs.ContainerImage.from_registry("public.ecr.aws/appmesh/aws-appmesh-envoy:v1.23.1.0-prod"),
            container_name="envoy",
            memory_reservation_mib=256,
            environment={
                "REGION": os.getenv('AWS_DEFAULT_REGION'),
                "ENVOY_LOG_LEVEL": "info",
                "ENABLE_ENVOY_STATS_TAGS": "1",
                # "ENABLE_ENVOY_XRAY_TRACING": "1",
                "APPMESH_RESOURCE_ARN": mesh_vgw.virtual_gateway_arn
            },
            essential=True,
            logging=ecs.LogDriver.aws_logs(
                stream_prefix='/mesh-gateway',
                log_group=logGroup
            ),
            health_check=ecs.HealthCheck(
                command=["CMD-SHELL","curl -s http://localhost:9901/server_info | grep state | grep -q LIVE"],
            )
        )

        # Default port where frontend app is listening
        container.add_port_mappings(
            ecs.PortMapping(
                container_port=3000
            )
        )

        # For environment variables check https://docs.aws.amazon.com/app-mesh/latest/userguide/envoy-config.html
        mesh_gateway_proxy_fargate_service = ecs_patterns.NetworkLoadBalancedFargateService(
            self,
            "MeshGW-Proxy-Fargate-Service",
            service_name='mesh-gw-proxy',
            cpu=256,
            memory_limit_mib=512,
            desired_count=1,
            listener_port=80,
            assign_public_ip=True,
            task_definition=mesh_gw_proxy_task_def,
            cluster=ecs_cluster,
            public_load_balancer=True,
            cloud_map_options=ecs.CloudMapOptions(
                cloud_map_namespace=ecs_cluster.default_cloud_map_namespace,
                name='mesh-gw-proxy'
            )
        )

        # For testing purposes we will open any ipv4 requests to port 3000
        mesh_gateway_proxy_fargate_service.service.connections.allow_from_any_ipv4(
            port_range=aws_ec2.Port(protocol=aws_ec2.Protocol.TCP, string_representation="vtw_proxy", from_port=3000, to_port=3000),
            description="Allow NLB connections on port 3000"
        )

        mesh_gw_proxy_task_def.default_container.add_ulimits(ecs.Ulimit(
            hard_limit=15000,
            name=ecs.UlimitName.NOFILE,
            soft_limit=15000
            )
        )

        #Adding necessary policies for Envoy proxy to communicate with required services
        mesh_gw_proxy_task_def.execution_role.add_managed_policy(aws_iam.ManagedPolicy.from_aws_managed_policy_name("AmazonEC2ContainerRegistryReadOnly"))
        mesh_gw_proxy_task_def.execution_role.add_managed_policy(aws_iam.ManagedPolicy.from_aws_managed_policy_name("CloudWatchLogsFullAccess"))

        mesh_gw_proxy_task_def.task_role.add_managed_policy(aws_iam.ManagedPolicy.from_aws_managed_policy_name("CloudWatchFullAccess"))
        mesh_gw_proxy_task_def.task_role.add_managed_policy(aws_iam.ManagedPolicy.from_aws_managed_policy_name("AWSAppMeshEnvoyAccess"))

        mesh_gw_proxy_task_def.execution_role.add_to_policy(
            aws_iam.PolicyStatement(
                actions=['ec2:DescribeSubnets'],
                resources=['*']
            )
        )

        if xray:
            xray_container = mesh_gw_proxy_task_def.add_container(
                "FrontendServiceXrayContdef",
                image=ecs.ContainerImage.from_registry("amazon/aws-xray-daemon"),
                logging=ecs.LogDriver.aws_logs(
                    stream_prefix='/xray-container',
                    log_group=logGroup
                ),
                essential=True,
                container_name="xray",
                memory_reservation_mib=256,
                user="1337"
            )

            container.add_container_dependencies(ecs.ContainerDependency(
                  container=xray_container,
                  condition=ecs.ContainerDependencyCondition.START
              )
            )

            mesh_gw_proxy_task_def.task_role.add_managed_policy(aws_iam.ManagedPolicy.from_aws_managed_policy_name("AWSXRayDaemonWriteAccess"))

        CfnOutput(self, "MeshGwNlbDns",value=mesh_gateway_proxy_fargate_service.load_balancer.load_balancer_dns_name,export_name="MeshGwNlbDns")
        CfnOutput(self, "MeshArn",value=mesh.mesh_arn,export_name="MeshArn")
        CfnOutput(self, "MeshName",value=mesh.mesh_name,export_name="MeshName")
        CfnOutput(self, "MeshEnvoyServiceArn",value=mesh_gateway_proxy_fargate_service.service.service_arn,export_name="MeshEnvoyServiceArn")
        CfnOutput(self, "MeshVGWArn",value=mesh_vgw.virtual_gateway_arn,export_name="MeshVGWArn")
        CfnOutput(self, "MeshVGWName",value=mesh_vgw.virtual_gateway_name,export_name="MeshVGWName")
