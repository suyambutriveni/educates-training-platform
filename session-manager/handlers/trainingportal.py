import logging

import pykube
import kopf

from .helpers import xget, image_pull_policy, resource_owned_by

from .config import (
    OPERATOR_API_GROUP,
    OPERATOR_STATUS_KEY,
    OPERATOR_NAME_PREFIX,
    INGRESS_DOMAIN,
    INGRESS_PROTOCOL,
    INGRESS_SECRET,
    INGRESS_CLASS,
    CLUSTER_STORAGE_CLASS,
    CLUSTER_STORAGE_USER,
    CLUSTER_STORAGE_GROUP,
    CLUSTER_SECURITY_POLICY_ENGINE,
    GOOGLE_TRACKING_ID,
    ANALYTICS_WEBHOOK_URL,
    TRAINING_PORTAL_SCRIPT,
    TRAINING_PORTAL_STYLE,
    PORTAL_ADMIN_USERNAME,
    PORTAL_ADMIN_PASSWORD,
    PORTAL_ROBOT_USERNAME,
    PORTAL_ROBOT_PASSWORD,
    PORTAL_ROBOT_CLIENT_ID,
    PORTAL_ROBOT_CLIENT_SECRET,
    TRAINING_PORTAL_IMAGE,
)

__all__ = ["training_portal_create", "training_portal_delete"]

logger = logging.getLogger("educates")

api = pykube.HTTPClient(pykube.KubeConfig.from_env())


@kopf.on.create(
    f"training.{OPERATOR_API_GROUP}",
    "v1alpha1",
    "trainingportals",
    id=OPERATOR_STATUS_KEY,
    timeout=900,
)
def training_portal_create(name, uid, body, spec, status, patch, **_):
    # Calculate name for the portal namespace.

    portal_name = name
    portal_namespace = f"{portal_name}-ui"

    # Calculate access details for the portal. The hostname used to access the
    # portal can be overridden, but the namespace above is always the same.

    ingress_hostname = xget(spec, "portal.ingress.hostname")

    if not ingress_hostname:
        portal_hostname = f"{portal_name}-ui.{INGRESS_DOMAIN}"
    elif not "." in ingress_hostname:
        portal_hostname = f"{ingress_hostname}.{INGRESS_DOMAIN}"
    else:
        # If a FQDN is used it must still match the global ingress domain.
        portal_hostname = ingress_hostname

    portal_url = f"{INGRESS_PROTOCOL}://{portal_hostname}"

    # Calculate admin password and api credentials for portal management.

    admin_username = xget(
        spec, "portal.credentials.admin.username", PORTAL_ADMIN_USERNAME
    )
    admin_password = xget(
        spec, "portal.credentials.admin.password", PORTAL_ADMIN_PASSWORD
    )

    robot_username = xget(
        spec, "portal.credentials.robot.username", PORTAL_ROBOT_USERNAME
    )
    robot_password = xget(
        spec, "portal.credentials.robot.password", PORTAL_ROBOT_PASSWORD
    )

    robot_client_id = xget(spec, "portal.clients.robot.id", PORTAL_ROBOT_CLIENT_ID)
    robot_client_secret = xget(
        spec, "portal.clients.robot.secret", PORTAL_ROBOT_CLIENT_SECRET
    )

    # Calculate settigs for portal web interface.

    portal_title = xget(spec, "portal.title", "Workshops")
    portal_password = xget(spec, "portal.password", "")
    portal_index = xget(spec, "portal.index", "")
    portal_logo = xget(spec, "portal.logo", "")

    frame_ancestors = ",".join(xget(spec, "portal.theme.frame.ancestors", []))

    registration_type = xget(spec, "portal.registration.type", "one-step")
    enable_registration = str(xget(spec, "portal.registration.enabled", True)).lower()

    catalog_visibility = xget(spec, "portal.catalog.visibility", "private")

    google_tracking_id = xget(spec, "analytics.google.trackingId", GOOGLE_TRACKING_ID)

    analytics_webhook_url = xget(spec, "analytics.webhook.url", ANALYTICS_WEBHOOK_URL)

    # Create the namespace for holding the training portal. Before we attempt to
    # create the namespace, we first see whether it may already exist. This
    # could be because a prior namespace hadn't yet been deleted, or we failed
    # on a prior attempt to create the training portal some point after the
    # namespace had been created but before all other resources could be
    # created.

    try:
        namespace_instance = pykube.Namespace.objects(api).get(name=portal_namespace)

    except pykube.exceptions.ObjectDoesNotExist:
        # Namespace doesn't exist so we should be all okay to continue.

        pass

    except pykube.exceptions.KubernetesError:
        logger.exception(f"Unexpected error querying namespace {portal_namespace}.")

        patch["status"] = {OPERATOR_STATUS_KEY: {"phase": "Error"}}

        raise kopf.TemporaryError(
            f"Unexpected error querying namespace {portal_namespace}.", delay=30
        )

    else:
        # The namespace already exists. We need to check whether it is owned by
        # this training portal instance.

        if not resource_owned_by(namespace_instance.obj, body):
            # Namespace is owned by another party so we flag a transient error
            # and will check again later to give time for the namespace to be
            # deleted.

            patch["status"] = {OPERATOR_STATUS_KEY: {"phase": "Pending"}}

            raise kopf.TemporaryError(
                f"Namespace {portal_namespace} already exists.", delay=30
            )

        else:
            # We own the namespace so verify that our current state indicates we
            # previously had an error and want to retry. In this case we will
            # delete the namespace and flag a transient error again.

            phase = xget(status, f"{OPERATOR_STATUS_KEY}.phase")

            if phase == "Retrying":
                namespace_instance.delete()

                raise kopf.TemporaryError(
                    f"Deleting {portal_namespace} and retrying.", delay=30
                )

            else:
                patch["status"] = {OPERATOR_STATUS_KEY: {"phase": "Error"}}

                raise kopf.TemporaryError(
                    f"Training portal {portal_name} in unexpected state {phase}.",
                    delay=30,
                )

    # Namespace doesn't already exist so we need to create it. We query back
    # the namespace immediately so we can access ist unique uid. Note that we
    # set the owner of the namespace to be the training portal so deletion of
    # the training portal results in its deletion.

    namespace_body = {
        "apiVersion": "v1",
        "kind": "Namespace",
        "metadata": {
            "name": portal_namespace,
            "labels": {
                f"training.{OPERATOR_API_GROUP}/component": "portal",
                f"training.{OPERATOR_API_GROUP}/portal.name": portal_name,
            },
            "annotations": {"secretgen.carvel.dev/excluded-from-wildcard-matching": ""},
        },
    }

    kopf.adopt(namespace_body)

    try:
        pykube.Namespace(api, namespace_body).create()

        namespace_instance = pykube.Namespace.objects(api).get(name=portal_namespace)

    except pykube.exceptions.KubernetesError as e:
        logger.exception(f"Unexpected error creating namespace {portal_namespace}.")

        patch["status"] = {OPERATOR_STATUS_KEY: {"phase": "Retrying"}}

        raise kopf.TemporaryError(
            f"Failed to create namespace {portal_namespace}.", delay=30
        )

    # Delete any limit ranges applied to the namespace so they don't cause
    # issues with deploying the training portal. This can be an issue where
    # namespace/project templates apply them automatically to a namespace. The
    # problem is that we may do this query too quickly and they may not have
    # been created as yet.

    for limit_range in pykube.LimitRange.objects(api, namespace=portal_namespace).all():
        try:
            limit_range.delete()
        except pykube.exceptions.ObjectDoesNotExist:
            pass

    # Delete any resource quotas applied to the namespace so they don't cause
    # issues with deploying the training portal. This can be an issue where
    # namespace/project templates apply them automatically to a namespace. The
    # problem is that we may do this query too quickly and they may not have
    # been created as yet.

    for resource_quota in pykube.ResourceQuota.objects(
        api, namespace=portal_namespace
    ).all():
        try:
            resource_quota.delete()
        except pykube.exceptions.ObjectDoesNotExist:
            pass

    # Prepare all the resources required for the training portal web interface.
    # First up need to create a service account and bind required roles to it.
    # Note that we set the owner of the cluster role binding to be the namespace
    # so that deletion of the namespace results in its deletion.

    service_account_body = {
        "apiVersion": "v1",
        "kind": "ServiceAccount",
        "metadata": {
            "name": "training-portal",
            "namespace": portal_namespace,
            "labels": {
                f"training.{OPERATOR_API_GROUP}/component": "portal",
                f"training.{OPERATOR_API_GROUP}/portal.name": portal_name,
            },
        },
    }

    cluster_role_binding_body = {
        "apiVersion": "rbac.authorization.k8s.io/v1",
        "kind": "ClusterRoleBinding",
        "metadata": {
            "name": f"{OPERATOR_NAME_PREFIX}-training-portal-{portal_namespace}",
            "labels": {
                f"training.{OPERATOR_API_GROUP}/component": "portal",
                f"training.{OPERATOR_API_GROUP}/portal.name": portal_name,
            },
        },
        "roleRef": {
            "apiGroup": "rbac.authorization.k8s.io",
            "kind": "ClusterRole",
            "name": f"{OPERATOR_NAME_PREFIX}-training-portal",
        },
        "subjects": [
            {
                "kind": "ServiceAccount",
                "name": "training-portal",
                "namespace": portal_namespace,
            }
        ],
    }

    kopf.adopt(cluster_role_binding_body, namespace_instance.obj)

    psp_role_binding_body = {
        "apiVersion": "rbac.authorization.k8s.io/v1",
        "kind": "RoleBinding",
        "metadata": {
            "name": "training-portal-psp",
            "namespace": portal_namespace,
            "labels": {
                f"training.{OPERATOR_API_GROUP}/component": "portal",
                f"training.{OPERATOR_API_GROUP}/portal.name": portal_name,
            },
        },
        "roleRef": {
            "apiGroup": "rbac.authorization.k8s.io",
            "kind": "ClusterRole",
            "name": f"{OPERATOR_NAME_PREFIX}-training-portal-psp",
        },
        "subjects": [
            {
                "kind": "ServiceAccount",
                "name": "training-portal",
                "namespace": portal_namespace,
            }
        ],
    }

    persistent_volume_claim_body = {
        "apiVersion": "v1",
        "kind": "PersistentVolumeClaim",
        "metadata": {
            "name": "training-portal",
            "namespace": portal_namespace,
            "labels": {
                f"training.{OPERATOR_API_GROUP}/component": "portal",
                f"training.{OPERATOR_API_GROUP}/portal.name": portal_name,
            },
        },
        "spec": {
            "accessModes": ["ReadWriteOnce"],
            "resources": {"requests": {"storage": "1Gi"}},
        },
    }

    if CLUSTER_STORAGE_CLASS:
        persistent_volume_claim_body["spec"]["storageClassName"] = CLUSTER_STORAGE_CLASS

    config_map_body = {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {
            "name": "training-portal",
            "namespace": portal_namespace,
            "labels": {
                f"training.{OPERATOR_API_GROUP}/component": "portal",
                f"training.{OPERATOR_API_GROUP}/portal.name": portal_name,
            },
        },
        "data": {
            "logo": portal_logo,
            "theme.js": TRAINING_PORTAL_SCRIPT,
            "theme.css": TRAINING_PORTAL_STYLE,
        },
    }

    deployment_body = {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {
            "name": "training-portal",
            "namespace": portal_namespace,
            "labels": {
                f"training.{OPERATOR_API_GROUP}/component": "portal",
                f"training.{OPERATOR_API_GROUP}/portal.name": portal_name,
                f"training.{OPERATOR_API_GROUP}/portal.services.dashboard": "true",
            },
        },
        "spec": {
            "replicas": 1,
            "selector": {"matchLabels": {"deployment": "training-portal"}},
            "strategy": {"type": "Recreate"},
            "template": {
                "metadata": {
                    "labels": {
                        "deployment": "training-portal",
                        f"training.{OPERATOR_API_GROUP}/component": "portal",
                        f"training.{OPERATOR_API_GROUP}/portal.name": portal_name,
                        f"training.{OPERATOR_API_GROUP}/portal.services.dashboard": "true",
                    },
                },
                "spec": {
                    "serviceAccountName": "training-portal",
                    "securityContext": {
                        "runAsUser": 1001,
                        "fsGroup": CLUSTER_STORAGE_GROUP,
                        "supplementalGroups": [CLUSTER_STORAGE_GROUP],
                    },
                    "containers": [
                        {
                            "name": "portal",
                            "image": TRAINING_PORTAL_IMAGE,
                            "imagePullPolicy": image_pull_policy(TRAINING_PORTAL_IMAGE),
                            "resources": {
                                "requests": {"memory": "256Mi"},
                                "limits": {"memory": "256Mi"},
                            },
                            "ports": [{"containerPort": 8080, "protocol": "TCP"}],
                            "readinessProbe": {
                                "httpGet": {"path": "/accounts/login/", "port": 8080},
                                "initialDelaySeconds": 10,
                                "periodSeconds": 10,
                            },
                            "livenessProbe": {
                                "httpGet": {"path": "/accounts/login/", "port": 8080},
                                "initialDelaySeconds": 15,
                                "periodSeconds": 10,
                            },
                            "env": [
                                {
                                    "name": "OPERATOR_API_GROUP",
                                    "value": OPERATOR_API_GROUP,
                                },
                                {
                                    "name": "OPERATOR_STATUS_KEY",
                                    "value": OPERATOR_STATUS_KEY,
                                },
                                {
                                    "name": "OPERATOR_NAME_PREFIX",
                                    "value": OPERATOR_NAME_PREFIX,
                                },
                                {
                                    "name": "TRAINING_PORTAL",
                                    "value": portal_name,
                                },
                                {
                                    "name": "PORTAL_UID",
                                    "value": uid,
                                },
                                {
                                    "name": "PORTAL_HOSTNAME",
                                    "value": portal_hostname,
                                },
                                {
                                    "name": "PORTAL_TITLE",
                                    "value": portal_title,
                                },
                                {
                                    "name": "PORTAL_PASSWORD",
                                    "value": portal_password,
                                },
                                {
                                    "name": "PORTAL_INDEX",
                                    "value": portal_index,
                                },
                                {
                                    "name": "FRAME_ANCESTORS",
                                    "value": frame_ancestors,
                                },
                                {
                                    "name": "ADMIN_USERNAME",
                                    "value": admin_username,
                                },
                                {
                                    "name": "ADMIN_PASSWORD",
                                    "value": admin_password,
                                },
                                {
                                    "name": "INGRESS_DOMAIN",
                                    "value": INGRESS_DOMAIN,
                                },
                                {
                                    "name": "REGISTRATION_TYPE",
                                    "value": registration_type,
                                },
                                {
                                    "name": "ENABLE_REGISTRATION",
                                    "value": enable_registration,
                                },
                                {
                                    "name": "CATALOG_VISIBILITY",
                                    "value": catalog_visibility,
                                },
                                {
                                    "name": "INGRESS_CLASS",
                                    "value": INGRESS_CLASS,
                                },
                                {
                                    "name": "INGRESS_PROTOCOL",
                                    "value": INGRESS_PROTOCOL,
                                },
                                {
                                    "name": "INGRESS_SECRET",
                                    "value": INGRESS_SECRET,
                                },
                                {
                                    "name": "GOOGLE_TRACKING_ID",
                                    "value": google_tracking_id,
                                },
                                {
                                    "name": "ANALYTICS_WEBHOOK_URL",
                                    "value": analytics_webhook_url
                                }
                            ],
                            "volumeMounts": [
                                {"name": "data", "mountPath": "/opt/app-root/data"},
                                {"name": "config", "mountPath": "/opt/app-root/config"},
                            ],
                        }
                    ],
                    "volumes": [
                        {
                            "name": "data",
                            "persistentVolumeClaim": {"claimName": "training-portal"},
                        },
                        {
                            "name": "config",
                            "configMap": {"name": "training-portal"},
                        },
                    ],
                },
            },
        },
    }

    if CLUSTER_STORAGE_USER:
        # This hack is to cope with Kubernetes clusters which don't properly set
        # up persistent volume ownership. IBM Kubernetes is one example. The
        # init container runs as root and sets permissions on the storage and
        # ensures it is group writable. Note that this will only work where pod
        # security policies are not enforced. Don't attempt to use it if they
        # are. If they are, this hack should not be required.

        storage_init_container = {
            "name": "storage-permissions-initialization",
            "image": TRAINING_PORTAL_IMAGE,
            "imagePullPolicy": image_pull_policy(TRAINING_PORTAL_IMAGE),
            "securityContext": {"runAsUser": 0},
            "command": ["/bin/sh", "-c"],
            "args": [
                f"chown {CLUSTER_STORAGE_USER}:{CLUSTER_STORAGE_GROUP} /mnt && chmod og+rwx /mnt"
            ],
            "resources": {
                "requests": {"memory": "256Mi"},
                "limits": {"memory": "256Mi"},
            },
            "volumeMounts": [{"name": "data", "mountPath": "/mnt"}],
        }

        deployment_body["spec"]["template"]["spec"]["initContainers"] = [
            storage_init_container
        ]

    service_body = {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {
            "name": "training-portal",
            "namespace": portal_namespace,
            "labels": {
                f"training.{OPERATOR_API_GROUP}/component": "portal",
                f"training.{OPERATOR_API_GROUP}/portal.name": portal_name,
            },
        },
        "spec": {
            "type": "ClusterIP",
            "ports": [{"port": 8080, "protocol": "TCP", "targetPort": 8080}],
            "selector": {"deployment": "training-portal"},
        },
    }

    ingress_body = {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "Ingress",
        "metadata": {
            "name": "training-portal",
            "namespace": portal_namespace,
            "labels": {
                f"training.{OPERATOR_API_GROUP}/component": "portal",
                f"training.{OPERATOR_API_GROUP}/portal.name": portal_name,
            },
            "annotations": {},
        },
        "spec": {
            "rules": [
                {
                    "host": portal_hostname,
                    "http": {
                        "paths": [
                            {
                                "path": "/",
                                "pathType": "Prefix",
                                "backend": {
                                    "service": {
                                        "name": "training-portal",
                                        "port": {"number": 8080},
                                    }
                                },
                            }
                        ]
                    },
                }
            ]
        },
    }

    if INGRESS_CLASS:
        ingress_body["metadata"]["annotations"][
            "kubernetes.io/ingress.class"
        ] = INGRESS_CLASS

    if INGRESS_PROTOCOL == "https":
        ingress_body["metadata"]["annotations"].update(
            {
                "ingress.kubernetes.io/force-ssl-redirect": "true",
                "nginx.ingress.kubernetes.io/ssl-redirect": "true",
                "nginx.ingress.kubernetes.io/force-ssl-redirect": "true",
            }
        )

    if INGRESS_SECRET:
        ingress_body["spec"]["tls"] = [
            {
                "hosts": [portal_hostname],
                "secretName": INGRESS_SECRET,
            }
        ]

    # Create all the resources and if we fail on any then flag a transient
    # error and we will retry again later. Note that we create the deployment
    # last so no workload is created unless everything else worked okay.

    try:
        pykube.ServiceAccount(api, service_account_body).create()
        pykube.ClusterRoleBinding(api, cluster_role_binding_body).create()
        pykube.PersistentVolumeClaim(api, persistent_volume_claim_body).create()
        pykube.ConfigMap(api, config_map_body).create()
        pykube.Service(api, service_body).create()
        pykube.Ingress(api, ingress_body).create()

        if CLUSTER_SECURITY_POLICY_ENGINE == "psp":
            pykube.RoleBinding(api, psp_role_binding_body).create()

        pykube.Deployment(api, deployment_body).create()

    except pykube.exceptions.KubernetesError as e:
        logger.exception(f"Unexpected error creating training portal {portal_name}.")

        patch["status"] = {OPERATOR_STATUS_KEY: {"phase": "Retrying"}}

        raise kopf.TemporaryError(
            f"Unexpected error creating training portal {portal_name}.", delay=30
        )

    # Save away the details of the portal which was created in status.

    return {
        "phase": "Running",
        "namespace": portal_namespace,
        "url": portal_url,
        "credentials": {
            "admin": {"username": admin_username, "password": admin_password},
            "robot": {"username": robot_username, "password": robot_password},
        },
        "clients": {"robot": {"id": robot_client_id, "secret": robot_client_secret}},
    }


@kopf.on.delete(
    f"training.{OPERATOR_API_GROUP}", "v1alpha1", "trainingportals", optional=True
)
def training_portal_delete(**_):
    # Nothing to do here at this point because the owner references will ensure
    # that everything is cleaned up appropriately.

    pass