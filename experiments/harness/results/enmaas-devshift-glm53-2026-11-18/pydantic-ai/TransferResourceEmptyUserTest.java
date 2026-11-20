package com.projectlightpath.fixture;

import static io.restassured.RestAssured.given;
import static org.hamcrest.CoreMatchers.is;

import io.quarkus.test.junit.QuarkusTest;
import io.restassured.http.ContentType;
import org.junit.jupiter.api.Test;

/**
 * Verifies that POST /transfers with an empty user string is still processed
 * successfully (HTTP 200) and echoes the request reference back, but is not
 * accepted, since the target auth check ({@code UserAuthPublicKey#doAuth})
 * rejects a blank user.
 */
@QuarkusTest
class TransferResourceEmptyUserTest {

    @Test
    void returnsNotAcceptedForEmptyUser() {
        given()
                .contentType(ContentType.JSON)
                .body("{\"requestReference\":\"req-5050\",\"user\":\"\"}")
                .when()
                .post("/transfers")
                .then()
                .statusCode(200)
                .body("requestReference", is("req-5050"))
                .body("accepted", is(false));
    }
}
