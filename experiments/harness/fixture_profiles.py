"""Pinned application fixtures for the FLPATH-5050 comparison."""

from pathlib import Path
from typing import NamedTuple, Tuple

ROOT = Path(__file__).resolve().parent


class FixtureProfile(NamedTuple):
    name: str
    repository: str
    revision: str
    tree_sha256: str
    objective: Path
    objective_sha256: str
    source_paths: Tuple[str, ...]
    framework: str
    mutation_path: Path
    original_line: str
    mutated_line: str
    maven_image: str
    maven_image_id: str
    maven_options: Tuple[str, ...]
    staged: bool


QUARKUS = FixtureProfile(
    name="quarkus",
    repository="project-lightpath/java-analysis-adapter",
    revision="2aac181ae63d938c118bac133d33fb8ae19eeec9",
    tree_sha256="sha256:5e19aa18e2019ecf9fe2559648002bac97467da685f5ef3fb4eefa0251080efe",
    objective=ROOT / "fixtures/blank-user-objective.md",
    objective_sha256="sha256:b433ea91f94e46ab00a36f25a435b8471ebad93ce8d0113651678be49c1e3c02",
    source_paths=(
        "src/main/java/com/projectlightpath/fixture/api/TransferResource.java",
        "src/main/java/com/projectlightpath/fixture/service/DefaultTransferService.java",
        "src/main/java/com/projectlightpath/fixture/target/UserAuthPublicKey.java",
        "src/test/java/com/projectlightpath/fixture/TransferResourceTest.java",
    ),
    framework="Java 21 Quarkus",
    mutation_path=Path("src/main/java/com/projectlightpath/fixture/target/UserAuthPublicKey.java"),
    original_line="return strict && user != null && !user.isBlank();",
    mutated_line="return true;",
    maven_image="docker.io/library/maven:3.9.11-eclipse-temurin-21",
    maven_image_id="b9611ad9b2f6ebef1c9369a32d794718fc28ebe2b9bb37b81a64b731370907bd",
    maven_options=("-Ptest",),
    staged=False,
)

SPRING_TRANSFER = FixtureProfile(
    name="spring-transfer",
    repository="lzspangler/spring-boot-example-application",
    revision="01a18a1b40e151ffcc1d7bef86bf335f87999750",
    tree_sha256="sha256:775546dd47b43d97cb295610b002d35aa2556a626d4fc4a9213ad342a61f06c6",
    objective=ROOT / "fixtures/spring-transfer-objective.md",
    objective_sha256="sha256:6a3d694c7e91803d12266fcc1244a8692ef0d45c724838f35b07827ad417708a",
    source_paths=(
        "pom.xml",
        "src/main/java/com/redhat/lightwell/service/TransactionService.java",
        "src/main/java/com/redhat/lightwell/model/dto/TransferRequest.java",
        "src/main/java/com/redhat/lightwell/model/Account.java",
        "src/test/java/com/redhat/lightwell/service/TransactionServiceTest.java",
    ),
    framework="Java 17 Spring Boot 2.7",
    mutation_path=Path("src/main/java/com/redhat/lightwell/service/TransactionService.java"),
    original_line="if (source.getBalance().compareTo(request.getAmount()) < 0) {",
    mutated_line="if (source.getBalance().compareTo(request.getAmount()) <= 0) {",
    maven_image="docker.io/library/maven:3.9.11-eclipse-temurin-17",
    maven_image_id="bbb7e05a6487b189e3dc833b6360b4f9eaf0154299fb4e67e764cad5cca33800",
    maven_options=(),
    staged=True,
)

QUARKUS_STAGED = FixtureProfile(
    name="quarkus-staged",
    repository=QUARKUS.repository,
    revision=QUARKUS.revision,
    tree_sha256=QUARKUS.tree_sha256,
    objective=ROOT / "fixtures/quarkus-blank-user-staged-objective.md",
    objective_sha256="sha256:4d93192ab4544aace71c1e41d2dc6b88ba00f1bbf920901a7a3e0d41675cd7ec",
    source_paths=("pom.xml",) + QUARKUS.source_paths,
    framework=QUARKUS.framework,
    mutation_path=QUARKUS.mutation_path,
    original_line=QUARKUS.original_line,
    mutated_line=QUARKUS.mutated_line,
    maven_image=QUARKUS.maven_image,
    maven_image_id=QUARKUS.maven_image_id,
    maven_options=QUARKUS.maven_options,
    staged=True,
)

SPRING_XML_DEPOSIT = FixtureProfile(
    name="spring-xml-deposit",
    repository=SPRING_TRANSFER.repository,
    revision=SPRING_TRANSFER.revision,
    tree_sha256=SPRING_TRANSFER.tree_sha256,
    objective=ROOT / "fixtures/spring-xml-deposit-objective.md",
    objective_sha256="sha256:acbbfbec9fa43de34e70f31c7fd34b7dcfa3d8b2d76601acc975c1cbf86c99c9",
    source_paths=(
        "pom.xml",
        "src/main/java/com/redhat/lightwell/service/StatementImportService.java",
        "src/main/java/com/redhat/lightwell/model/Account.java",
        "src/main/java/com/redhat/lightwell/model/Transaction.java",
        "src/test/java/com/redhat/lightwell/service/StatementImportServiceTest.java",
        "src/test/java/com/redhat/lightwell/integration/StatementImportIntegrationTest.java",
    ),
    framework=SPRING_TRANSFER.framework,
    mutation_path=Path("src/main/java/com/redhat/lightwell/service/StatementImportService.java"),
    original_line='"DEPOSIT".equalsIgnoreCase(type)',
    mutated_line='"DEPOSIT".equals(type)',
    maven_image=SPRING_TRANSFER.maven_image,
    maven_image_id=SPRING_TRANSFER.maven_image_id,
    maven_options=SPRING_TRANSFER.maven_options,
    staged=True,
)

PROFILES = {profile.name: profile for profile in
            (QUARKUS, QUARKUS_STAGED, SPRING_TRANSFER, SPRING_XML_DEPOSIT)}


def get_profile(name: str) -> FixtureProfile:
    try:
        return PROFILES[name]
    except KeyError:
        raise ValueError(f"unknown fixture profile: {name}")
