package com.projectlightpath.fixture;

import static io.restassured.RestAssured.given;
import static org.hamcrest.CoreMatchers.is;

import io.quarkus.test.junit.QuarkusTest;
import io.restassured.http.ContentType;
import org.junit.jupiter.api.Test;

@QuarkusTest
class TransferGeneratedReplayTest {
    @Test
    void returnsTheExpectedAcceptedTransferResponse() {
        given()
                .contentType(ContentType.JSON)
                .body("{\"requestReference\":\"flpath-5050-replay-001\",\"user\":\"alice\"}")
                .when()
                .post("/transfers")
                .then()
                .statusCode(200)
                .body("requestReference", is("flpath-5050-replay-001"))
                .body("accepted", is(true))
                .body("path", is("resource->service->auth->target"));
    }
}
