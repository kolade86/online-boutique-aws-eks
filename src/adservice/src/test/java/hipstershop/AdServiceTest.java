package hipstershop;

import static org.junit.jupiter.api.Assertions.*;

import hipstershop.Demo.Ad;
import hipstershop.Demo.AdRequest;
import hipstershop.Demo.AdResponse;
import io.grpc.ManagedChannel;
import io.grpc.Server;
import io.grpc.inprocess.InProcessChannelBuilder;
import io.grpc.inprocess.InProcessServerBuilder;
import java.io.IOException;
import java.util.List;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;

class AdServiceTest {

  private Server server;
  private ManagedChannel channel;
  private AdServiceGrpc.AdServiceBlockingStub blockingStub;

  @BeforeEach
  void setUp() throws IOException {
    String serverName = InProcessServerBuilder.generateName();

    server =
        InProcessServerBuilder.forName(serverName)
            .directExecutor()
            .addService(new AdService.AdServiceImpl())
            .build()
            .start();

    channel = InProcessChannelBuilder.forName(serverName).directExecutor().build();
    blockingStub = AdServiceGrpc.newBlockingStub(channel);
  }

  @AfterEach
  void tearDown() {
    channel.shutdownNow();
    server.shutdownNow();
  }

  @Test
  void getAds_withClothingContext_returnsRelevantAds() {
    AdRequest request = AdRequest.newBuilder().addContextKeys("clothing").build();
    AdResponse response = blockingStub.getAds(request);

    assertFalse(response.getAdsList().isEmpty());
    for (Ad ad : response.getAdsList()) {
      assertNotNull(ad.getRedirectUrl());
      assertNotNull(ad.getText());
    }
  }

  @Test
  void getAds_withKitchenContext_returnsKitchenAds() {
    AdRequest request = AdRequest.newBuilder().addContextKeys("kitchen").build();
    AdResponse response = blockingStub.getAds(request);

    // kitchen category has 2 ads (bamboo glass jar + mug)
    assertEquals(2, response.getAdsList().size());
  }

  @Test
  void getAds_withNoContext_returnsRandomAds() {
    AdRequest request = AdRequest.newBuilder().build();
    AdResponse response = blockingStub.getAds(request);

    // Should return MAX_ADS_TO_SERVE (2) random ads
    assertEquals(2, response.getAdsList().size());
  }

  @Test
  void getAds_withUnknownContext_fallsBackToRandom() {
    AdRequest request = AdRequest.newBuilder().addContextKeys("nonexistent").build();
    AdResponse response = blockingStub.getAds(request);

    // Unknown category returns empty, so falls back to random (2 ads)
    assertEquals(2, response.getAdsList().size());
  }

  @Test
  void getAds_withMultipleContexts_returnsAllMatching() {
    AdRequest request =
        AdRequest.newBuilder().addContextKeys("clothing").addContextKeys("hair").build();
    AdResponse response = blockingStub.getAds(request);

    // clothing has 1 ad (tank top), hair has 1 ad (hairdryer) = 2 total
    assertEquals(2, response.getAdsList().size());
  }

  @Test
  void getAds_adFieldsArePopulated() {
    AdRequest request = AdRequest.newBuilder().addContextKeys("footwear").build();
    AdResponse response = blockingStub.getAds(request);

    assertEquals(1, response.getAdsList().size());
    Ad ad = response.getAds(0);
    assertEquals("/product/L9ECAV7KIM", ad.getRedirectUrl());
    assertTrue(ad.getText().contains("Loafers"));
  }

  @Test
  void getAds_allCategories_returnAds() {
    List<String> categories =
        List.of("clothing", "accessories", "footwear", "hair", "decor", "kitchen");

    for (String category : categories) {
      AdRequest request = AdRequest.newBuilder().addContextKeys(category).build();
      AdResponse response = blockingStub.getAds(request);
      assertFalse(
          response.getAdsList().isEmpty(), "Category '" + category + "' should return ads");
    }
  }
}
