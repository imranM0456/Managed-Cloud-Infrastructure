#!/bin/bash
  export TF_VAR_subscription_id=a3496361-d913-432d-98bd-98bd6cae7ed4
terraform plan -parallelism=1 -var="subscription_id=a3496361-d913-432d-98bd-98bd6cae7ed4"