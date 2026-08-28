// swift-tools-version: 6.0
import PackageDescription

let package = Package(
  name: "SEECODERDesktop",
  platforms: [.macOS(.v14)],
  products: [.executable(name: "SEECODERDesktop", targets: ["SEECODERDesktop"])],
  targets: [.executableTarget(name: "SEECODERDesktop", resources: [.copy("Resources")])]
)
