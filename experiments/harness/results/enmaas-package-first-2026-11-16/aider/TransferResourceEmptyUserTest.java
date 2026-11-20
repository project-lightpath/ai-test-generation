package com.projectlightpath.fixture;

import static io.restassured.RestAssured.given;
import static org.hamcrest.CoreMatchers.is;

import io.quarkus.test.junit.QuarkusTest;
import io.restassured.http.ContentType;
import org.junit.jupiter.api.Test;

/**
 * Verifies that a transfer request with an empty {@code user} field is processed
 * but not accepted. The requestReference must be preserved in the response.
 */
@QuarkusTest
class TransferResourceEmptyUserTest {

    @Test
    void emptyUserIsNotAccepted() {
        given()
                .contentType(ContentType.JSON)
                .body("{\"requestReference\":\"req-002\",\"user\":\"\"}")
                .when()
                .post("/transfers")
                .then()
                .statusCode(200)
                .body("requestReference", is("req-002"))
                .body("accepted", is(false));
    }
}
