targetScope = 'subscription'

@minLength(1)
@maxLength(64)
@description('Name of the environment (e.g., dev, prod)')
param environmentName string

@description('Primary location for all resources')
param location string = 'eastus2'

var resourceGroupName = 'rg-${environmentName}'
var tags = {
  'azd-env-name': environmentName
}

// Unique suffix for globally unique names
var resourceToken = toLower(uniqueString(subscription().id, environmentName, location))

resource rg 'Microsoft.Resources/resourceGroups@2024-03-01' = {
  name: resourceGroupName
  location: location
  tags: tags
}

module resources 'resources.bicep' = {
  name: 'resources'
  scope: rg
  params: {
    location: location
    tags: tags
    resourceToken: resourceToken
    environmentName: environmentName
  }
}

output AZURE_OPENAI_ENDPOINT string = resources.outputs.openaiEndpoint
output AZURE_RESOURCE_GROUP string = rg.name
output AZURE_CONTAINER_REGISTRY_ENDPOINT string = resources.outputs.containerRegistryEndpoint
output AZURE_CONTAINER_REGISTRY_NAME string = resources.outputs.containerRegistryName
output SERVICE_HOV_APP_URI string = resources.outputs.containerAppUri
